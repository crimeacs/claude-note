"""Sync primary-calendar events. Uses the stored OAuth token in token.json."""
import json, sys

CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"


class HttpError(Exception):
    pass


def events(token):
    # Google checks the scopes the token was GRANTED, not the ones the app now requests.
    if CALENDAR not in token["scopes"]:
        raise HttpError('<HttpError 403 when requesting https://www.googleapis.com/calendar/v3/'
                        'calendars/primary/events returned "Request had insufficient authentication '
                        'scopes.". Details: "[{\'reason\': \'insufficientPermissions\'}]">')
    return []


if __name__ == "__main__":
    token = json.load(open("token.json"))
    print("Syncing calendar events ...")
    try:
        events(token)
    except HttpError as e:
        print(f"googleapiclient.errors.HttpError: {e}", file=sys.stderr)
        sys.exit(1)
    print("Synced.")
