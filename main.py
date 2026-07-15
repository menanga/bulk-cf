#!/usr/bin/env python3
"""
Cloudflare Auto Signup — Batch Processor

Automates the full pipeline in batches:
1. Generate email from domains.txt
2. Sign up for Cloudflare account
3. Verify email via Gmail IMAP
4. Create Workers AI token
5. Validate token
6. Batch deploy to 9router

Usage:
    python main.py --gmail-user user@gmail.com --gmail-password pass --nine-router-password pass
    python main.py --batch-size 5 --delay-account 300 --delay-batch 1800 --headless
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from src.email_generator import EmailGenerator
from src.email_verifier import verify_cloudflare_email
from src.nine_router_client import NineRouterClient
from src.signup_flow import signup
from src.token_creator import create_token
from src.token_validator import validate_token


console = Console()


def load_config(config_path="config.json"):
    """Load config from file, fallback to empty dict."""
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON in config: {e}[/red]")
        return {}


def load_env_or_config(env_key, config_keys, default=None):
    """Load value from env var first, then config.json, then default."""
    env_val = os.getenv(env_key)
    if env_val is not None:
        return env_val

    config = load_config()
    for key_path in config_keys:
        val = config
        for key in key_path.split("."):
            val = val.get(key, {})
        if val and not isinstance(val, dict):
            return val

    return default


def setup_domains_from_env(domains_file: str = "domains.txt"):
    """Create/update domains file from DOMAINS env var when set."""
    domains_env = os.getenv("DOMAINS")
    if domains_env:
        domains = [d.strip() for d in domains_env.replace(",", "\n").split("\n") if d.strip()]
        Path(domains_file).write_text("\n".join(domains), encoding="utf-8")
        console.print(f"[cyan]Updated {domains_file} from env with {len(domains)} domains[/cyan]")


async def process_account(
    email_gen: EmailGenerator,
    gmail_user: str,
    gmail_password: str,
    headless: bool,
    progress,
    task_id,
) -> dict:
    """
    Process single account through full pipeline.

    Returns:
        dict with success, email, token, account_id, or error
    """
    logger = logging.getLogger(__name__)

    try:
        # Step 1: Generate email
        progress.update(task_id, description="[cyan]Generating email...")
        email = email_gen.generate_email()
        logger.info(f"✓ Step 1: Generated email: {email}")

        # Step 2: Signup
        progress.update(task_id, description=f"[cyan]Signing up: {email}")
        logger.info(f"→ Step 2: Starting signup for {email}")

        import nodriver as uc
        from src.utils import generate_password

        password = generate_password()

        # Docker/non-root needs no_sandbox flag
        browser_config = uc.Config()
        browser_config.headless = headless

        if os.getenv("DOCKER_ENV") or (hasattr(os, 'geteuid') and os.geteuid() != 0):
            browser_config.sandbox = False

        browser = await uc.start(config=browser_config)
        page = await browser.get("https://dash.cloudflare.com/sign-up")

        # Retry signup with exponential backoff on rate limit
        max_retries = 3
        for attempt in range(max_retries):
            signup_result = await signup(page=page, email=email, password=password, max_wait=60)

            if signup_result.success:
                break

            if "unable to sign up" in signup_result.error.lower():
                if attempt < max_retries - 1:
                    wait_time = 30 * (2 ** attempt)
                    logger.warning(f"Rate limited. Waiting {wait_time}s before retry {attempt+2}/{max_retries}")
                    await asyncio.sleep(wait_time)
                    continue

            # Non-rate-limit error or final retry
            logger.error(f"✗ Step 2: Signup failed - {signup_result.error}")
            try:
                await browser.stop()
            except:
                pass
            return {"success": False, "email": email, "error": signup_result.error}

        if not signup_result.success:
            logger.error(f"✗ Step 2: All retries exhausted")
            try:
                await browser.stop()
            except:
                pass
            return {"success": False, "email": email, "error": "Max retries exceeded"}

        password = signup_result.password
        account_id = signup_result.account_id
        logger.info(f"✓ Step 2: Signup OK - account_id: {account_id}")

        # Step 3: Verify email (keep browser open to preserve session)
        progress.update(task_id, description=f"[cyan]Verifying email: {email}")
        logger.info(f"→ Step 3: Verifying email via IMAP")
        verify_result = await verify_cloudflare_email(email, gmail_user, gmail_password, timeout=180)

        if not verify_result.success or not verify_result.link:
            logger.error(f"✗ Step 3: Email verification failed - {verify_result.error}")
            try:
                await browser.stop()
            except:
                pass
            return {"success": False, "email": email, "error": verify_result.error or "Email verification timeout"}

        verification_link = verify_result.link
        logger.info(f"✓ Step 3: Verification link received, navigating...")

        # Navigate verification link in existing browser to activate account
        await page.get(verification_link)
        await asyncio.sleep(5)
        logger.info(f"✓ Step 3: Email verified")

        # Step 4: Create Workers AI token (browser session + verified account)
        progress.update(task_id, description=f"[cyan]Creating token: {email}")
        logger.info(f"→ Step 4: Creating Workers AI token")
        token_result = await create_token(page=page, account_id=account_id)

        # Close browser after token creation
        try:
            await browser.stop()
        except:
            pass

        if not token_result.success:
            logger.error(f"✗ Step 4: Token creation failed - {token_result.error}")
            return {"success": False, "email": email, "error": token_result.error}

        token = token_result.token
        logger.info(f"✓ Step 4: Token created - {token[:20]}...")

        # Step 5: Validate token
        progress.update(task_id, description=f"[cyan]Validating token: {account_id}")
        logger.info(f"→ Step 5: Validating token")
        validation = validate_token(token, account_id)

        if not validation.valid:
            logger.error(f"✗ Step 5: Token validation failed - {validation.error}")
            return {"success": False, "email": email, "error": validation.error}

        logger.info(f"✓ Step 5: Token valid")

        progress.update(task_id, description=f"[green]Complete: {email}")
        logger.info(f"✓ Account pipeline complete: {email}")

        return {
            "success": True,
            "email": email,
            "token": token,
            "account_id": account_id,
        }

    except Exception as e:
        logger.exception(f"Account processing error: {e}")
        return {"success": False, "email": email if 'email' in locals() else "unknown", "error": str(e)}



async def main():
    """Main batch processing orchestrator."""
    parser = argparse.ArgumentParser(description="Cloudflare Batch Signup")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config file")
    parser.add_argument("--domains", type=str, help="Path to domains file")
    parser.add_argument("--max-accounts", type=int, help="Maximum total accounts to create")
    parser.add_argument("--batch-size", type=int, help="Accounts per batch")
    parser.add_argument("--delay-account", type=int, help="Delay between accounts (seconds)")
    parser.add_argument("--delay-batch", type=int, help="Delay between batches (seconds)")
    parser.add_argument("--headless", action="store_true", help="Run browsers headless")
    parser.add_argument("--gmail-user", type=str, help="Gmail address for IMAP verification")
    parser.add_argument("--gmail-password", type=str, help="Gmail password")
    parser.add_argument("--nine-router-password", type=str, help="9router dashboard password")
    parser.add_argument("--nine-router-url", type=str, help="9router API URL")
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Load from env first, then config, then CLI args (CLI highest priority)
    gmail_user = args.gmail_user or load_env_or_config("GMAIL_EMAIL", ["gmail.user"])
    gmail_password = args.gmail_password or load_env_or_config("GMAIL_APP_PASSWORD", ["gmail.password"])
    nine_router_password = args.nine_router_password or load_env_or_config("NINE_ROUTER_PASSWORD", ["nine_router.password"])
    nine_router_url = load_env_or_config("NINE_ROUTER_URL", ["nine_router.api_url"], "https://my-9router-or-omniroute.com/api")

    domains_file = args.domains or load_env_or_config("DOMAINS_FILE", ["domains_file"], "domains.txt")

    # Setup domains file from env when set
    setup_domains_from_env(domains_file)

    max_accounts = args.max_accounts if args.max_accounts is not None else (int(load_env_or_config("MAX_ACCOUNTS", ["batch.max_accounts"], "0") or 0) or None)
    batch_size = args.batch_size if args.batch_size is not None else int(load_env_or_config("BATCH_SIZE", ["batch.batch_size"], "10"))
    delay_account = args.delay_account if args.delay_account is not None else int(load_env_or_config("DELAY_ACCOUNT", ["batch.delay_account"], "600"))
    delay_batch = args.delay_batch if args.delay_batch is not None else int(load_env_or_config("DELAY_BATCH", ["batch.delay_batch"], "3600"))
    headless = args.headless if args.headless else (load_env_or_config("HEADLESS", ["batch.headless"], "false").lower() in ("true", "1", "yes"))

    if not gmail_user or not gmail_password:
        console.print("[red]Error: Gmail credentials required (config.json or CLI)[/red]")
        sys.exit(1)

    if not nine_router_password:
        console.print("[red]Error: 9router password required (config.json or CLI)[/red]")
        sys.exit(1)

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )

    # Fix Windows console encoding
    import sys
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    logger = logging.getLogger(__name__)

    console.print("[bold cyan]Cloudflare Batch Processor[/bold cyan]")
    if max_accounts:
        console.print(f"Max accounts: {max_accounts}")
    console.print(f"Batch size: {batch_size}")
    console.print(f"Delay account: {delay_account}s")
    console.print(f"Delay batch: {delay_batch}s")
    console.print(f"Headless: {headless}")
    console.print()

    # Initialize email generator
    with EmailGenerator(domains_file=domains_file) as email_gen:
        batch_num = 1
        total_accounts_attempted = 0
        total_accounts_success = 0

        try:
            while True:
                if max_accounts and total_accounts_attempted >= max_accounts:
                    console.print(f"[bold green]Max accounts limit reached ({max_accounts}). Stopping.[/bold green]")
                    break

                console.print(f"[bold yellow]Starting Batch {batch_num}[/bold yellow]")

                current_batch_size = batch_size
                if max_accounts:
                    remaining = max_accounts - total_accounts_attempted
                    current_batch_size = min(batch_size, remaining)
                entries = []


                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:

                    batch_task = progress.add_task(
                        f"[cyan]Batch {batch_num}",
                        total=args.batch_size
                    )

                    for i in range(current_batch_size):
                        account_task = progress.add_task(
                            f"[cyan]Account {i+1}/{current_batch_size}",
                            total=1
                        )

                        result = await process_account(
                            email_gen,
                            gmail_user,
                            gmail_password,
                            headless,
                            progress,
                            account_task,
                        )

                        if result["success"]:
                            entry = {
                                "name": f"CF-{result['account_id'][:8]}",
                                "email": result["email"],
                                "accountId": result["account_id"],
                                "apiKey": result["token"],
                                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                            entries.append(entry)

                            # Save to CSV immediately
                            csv_file = os.getenv("CSV_OUTPUT_PATH", "successful_accounts.csv")
                            file_exists = Path(csv_file).exists()

                            # Create directory if needed
                            Path(csv_file).parent.mkdir(parents=True, exist_ok=True)

                            with open(csv_file, 'a', encoding='utf-8') as f:
                                if not file_exists:
                                    f.write("timestamp,email,account_id,token\n")
                                f.write(f"{entry['timestamp']},{entry['email']},{entry['accountId']},{entry['apiKey']}\n")

                            console.print(f"[green]✓ {result['email']} — {result['account_id']}[/green]")
                        else:
                            console.print(f"[red]✗ {result['email']} — {result.get('error', 'Unknown')}[/red]")

                        progress.update(account_task, completed=1)
                        progress.update(batch_task, advance=1)

                        # Delay between accounts (except last)
                        if i < current_batch_size - 1:
                            console.print(f"[dim]Waiting {delay_account}s before next account...[/dim]")
                            time.sleep(delay_account)

                # Deploy batch to 9router
                if entries:
                    console.print(f"[bold cyan]Deploying {len(entries)} entries to 9router...[/bold cyan]")
                    try:
                        client = NineRouterClient()
                        auth_token = client.login(nine_router_password)

                        for entry in entries:
                            result = client.bulk_deploy(auth_token, [entry])
                            # 9router returns {success: N, failed: N, created: [...], errors: [...]}
                            if result.get("success", 0) > 0:
                                console.print(f"[green]✓ Deployed {entry['accountId']}[/green]")
                            else:
                                errors = result.get("errors", [])
                                console.print(f"[red]✗ Deploy failed {entry['accountId']}: {errors}[/red]")

                        console.print("[green]Batch deployment complete[/green]")
                    except Exception as e:
                        logger.exception(f"9router deployment error: {e}")
                        console.print(f"[red]Deployment error: {e}[/red]")
                else:
                    console.print("[yellow]No successful entries to deploy[/yellow]")

                success_count = len(entries)
                # Track attempts
                total_accounts_attempted += current_batch_size

                console.print(f"[bold cyan]Batch {batch_num} complete. Successful: {success_count}/{current_batch_size}[/bold cyan]")
                if max_accounts:
                    console.print(f"[bold cyan]Total attempted: {total_accounts_attempted}/{max_accounts}, Successful: {success_count}[/bold cyan]")
                console.print(f"[dim]Waiting {delay_batch}s before next batch...[/dim]")
                console.print()

                batch_num += 1
                time.sleep(delay_batch)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user[/yellow]")
            sys.exit(0)
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            console.print(f"[red]Fatal error: {e}[/red]")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
