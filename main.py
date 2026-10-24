"""Entrypoint for local one-shot testing and long-running GitHub Actions sessions."""
import argparse
import time
import traceback

from config import Settings
from state_store import StateStore, StateError
from engine import run_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Geopolitics News Bot")
    parser.add_argument("--once", action="store_true", help="Run one collection cycle and exit")
    parser.add_argument("--duration-minutes", type=int, default=None, help="Override service run duration")
    parser.add_argument("--interval-minutes", type=int, default=None, help="Override cycle interval")
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    duration_minutes = args.duration_minutes or settings.service_minutes
    interval_minutes = args.interval_minutes or settings.cycle_minutes

    print("[startup] Geopolitics News Bot starting (English posts only).")
    print(f"[startup] Website RSS configured: {'yes' if settings.website_rss_url else 'no'}")
    print(f"[startup] YouTube channel configured: {'yes' if settings.youtube_channel_id else 'no'}")
    print(f"[startup] Cycle={interval_minutes}m; max posts/cycle={settings.max_posts_per_cycle}; service={duration_minutes}m")

    try:
        state = StateStore(settings.github_repository, settings.github_token,
                           settings.state_branch, settings.max_state_items)
    except StateError as exc:
        print(f"[fatal] Cannot load safe dedupe state; refusing to post: {exc}")
        return 2

    if args.once:
        try:
            run_cycle(settings, state)
            return 0
        except Exception:
            traceback.print_exc()
            return 1

    end_at = time.monotonic() + duration_minutes * 60
    cycle_number = 0
    while time.monotonic() < end_at:
        cycle_number += 1
        started = time.monotonic()
        print(f"\n[cycle {cycle_number}] Starting UTC={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        try:
            run_cycle(settings, state)
        except StateError as exc:
            # If remote state can't be committed, stop. Posting without durable dedupe would be unsafe.
            print(f"[fatal] State persistence failed; stopping to avoid duplicate posts: {exc}")
            return 3
        except Exception as exc:
            print(f"[cycle] Unexpected error (will try next interval): {exc}")
            traceback.print_exc()
        remaining = end_at - time.monotonic()
        if remaining <= 0:
            break
        sleep_for = min(max(0, interval_minutes * 60 - (time.monotonic() - started)), remaining)
        if sleep_for > 0:
            print(f"[sleep] Waiting {sleep_for / 60:.1f} minutes before next source check.")
            time.sleep(sleep_for)

    print("[shutdown] Service window complete; next scheduled GitHub Actions run will start a fresh session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
