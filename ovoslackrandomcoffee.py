import argparse
import os
import random
from datetime import date
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
import time

load_dotenv()

slack_token = os.environ["SLACK_API_TOKEN"]
client = WebClient(token=slack_token)

EXCLUDE_LIST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "exclude_list.txt"
)


def load_exclude_list(path=EXCLUDE_LIST_PATH):
    if not os.path.exists(path):
        return set()
    entries = set()
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                entries.add(line)
    return entries


def resolve_excluded_ids(entries):
    if not entries:
        return set()
    ids = {e for e in entries if e.startswith(("U", "W"))}
    names = {e.lower().lstrip("@") for e in entries if not e.startswith(("U", "W"))}
    if not names:
        return ids
    try:
        cursor = None
        while True:
            response = client.users_list(cursor=cursor, limit=200)
            for user in response["members"]:
                profile = user.get("profile", {})
                candidates = {
                    user.get("name", "").lower(),
                    profile.get("display_name", "").lower(),
                    profile.get("display_name_normalized", "").lower(),
                    profile.get("real_name", "").lower(),
                    profile.get("real_name_normalized", "").lower(),
                }
                candidates.discard("")
                if candidates & names:
                    ids.add(user["id"])
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        print(f"Error resolving excluded names: {e}")
    return ids


def get_channel_members(channel_id):
    try:
        response = client.conversations_members(channel=channel_id)
        members = response["members"]
        bot_user_id = client.auth_test()["user_id"]  # get the bot's user ID
        members.remove(bot_user_id)  # remove the bot from the list of members
        return members
    except SlackApiError as e:
        print(f"Error getting channel members: {e}")
        return None


def generate_all_pairs(members):
    random.shuffle(members)
    return [members[i : i + 2] for i in range(0, len(members), 2)]


def post_pairs_to_channel(pairs, channel_id):
    for pair in pairs:
        if len(pair) == 2:
            message = f"Random coffee pair: <@{pair[0]}> and <@{pair[1]}>"
        else:
            message = f"<!channel> Poor <@{pair[0]}> has no buddy :slightly_frowning_face:, does anyone want to have two chats this week?"
        try:
            client.chat_postMessage(channel=channel_id, text=message)
        except SlackApiError as e:
            print(f"Error posting message: {e}")


def resolve_channel_id(env):
    var = "SLACK_CHANNEL" if env == "real" else "SLACK_DEV_CHANNEL"
    return os.environ[var]


def main(event, context):
    env = (event or {}).get("env") if isinstance(event, dict) else None
    if env is None:
        env = os.environ.get("COFFEEBOT_ENV")
    if env not in ("dev", "real"):
        raise ValueError("env must be 'dev' or 'real'")
    channel_id = resolve_channel_id(env)
    members = get_channel_members(channel_id)
    if members is None:
        print("Failed to get channel members.")
        return {"statusCode": 200, "body": "Failed to get channel members."}
    excluded_ids = resolve_excluded_ids(load_exclude_list())
    if excluded_ids:
        before = len(members)
        members = [m for m in members if m not in excluded_ids]
        print(f"Excluded {before - len(members)} member(s) from {EXCLUDE_LIST_PATH}.")
    pairs = generate_all_pairs(members)
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"Gathering pairs for the week of {date.today().strftime('%B %d')} ...",
        )
    except SlackApiError as e:
        print(f"Error posting message: {e}")
    post_pairs_to_channel(pairs, channel_id)
    return {"statusCode": 200, "body": "Calculating..."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["dev", "real"], required=True)
    args = parser.parse_args()
    main({"env": args.env}, None)
