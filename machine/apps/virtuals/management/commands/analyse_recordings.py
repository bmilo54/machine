from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.virtuals.screen_summary import analyse_pending, gemini_key


class Command(BaseCommand):
    help = (
        "Analyse new screen recordings with Gemini. "
        "Run on a schedule so Report fills in without clicking Write each time."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=48,
            help="Only look at recordings from the last N hours (default 48).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=1,
            help="How many recordings to analyse in this run (default 1).",
        )

    def handle(self, *args, **options):
        if not gemini_key():
            self.stderr.write("The Gemini key is missing from local.py.")
            return
        hours = max(1, options["hours"])
        limit = max(1, options["limit"])
        end = timezone.now()
        start = end - timedelta(hours=hours)
        self.stdout.write(
            "Looking for unread recordings from the last {} hour(s), up to {} this run.".format(
                hours, limit
            )
        )
        payload = analyse_pending(start, end, limit=limit)
        if not payload.get("ok"):
            self.stderr.write(payload.get("error") or "The analysis failed.")
            if payload.get("processed"):
                self.stdout.write("Processed {} before the error.".format(payload["processed"]))
            return
        processed = payload.get("processed") or 0
        remaining = payload.get("remaining") or 0
        if processed == 0 and remaining == 0:
            self.stdout.write("No unread recordings in this window.")
            return
        self.stdout.write(
            "Processed {}. {} still waiting.".format(processed, remaining)
        )
