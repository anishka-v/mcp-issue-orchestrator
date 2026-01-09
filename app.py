import os
import pathlib
import random
import zipfile
from io import BytesIO
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from rag import answer_query, delete_all_embeddings, index_slack_file_bytes

# Initializes your app with your bot token
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]   

app = App(token=BOT_TOKEN)
SAVE_DIR = pathlib.Path("./saved_files")
SAVE_DIR.mkdir(parents=True, exist_ok=True)

HEADERS = {"Authorization": f"Bearer {BOT_TOKEN}"}
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
INDEXED_FILE_IDS = set()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")

def download_slack_file(url_private_download: str) -> bytes:
    # 1) hit Slack URL without auto-redirects
    r = requests.get(url_private_download, headers=HEADERS, allow_redirects=False, timeout=60)

    # 2) If Slack redirects to workspace webapp ?redir=/files-pri/..., convert to files.slack.com path
    if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
        loc = r.headers["Location"]        
        r = requests.get(loc, headers=HEADERS, timeout=60)

    r.raise_for_status()

    # 3) refuse HTML (means you didn’t get file bytes)
    ct = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        raise RuntimeError(f"Got HTML, not file bytes. URL={r.url}")

    return r.content


def create_github_issue(title: str, body: str) -> tuple[int, str]:
    if not (GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO):
        raise RuntimeError("Missing GitHub configuration. Set GITHUB_TOKEN, GITHUB_OWNER, and GITHUB_REPO.")

    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    issue = r.json()
    return issue["number"], issue["html_url"]

BOT_USER_ID = None

@app.event("app_home_opened")
def _cache_bot_id(event, client, logger):
    global BOT_USER_ID
    if BOT_USER_ID:
        return
    BOT_USER_ID = client.auth_test()["user_id"]
    logger.info(f"Cached BOT_USER_ID={BOT_USER_ID}")

@app.event("message")
def on_message(event, client, logger):
    global BOT_USER_ID

    # Ignore bot messages
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return

    # Ensure bot id cached (works even if app_home_opened didn't happen)
    if not BOT_USER_ID:
        BOT_USER_ID = client.auth_test()["user_id"]
        print (BOT_USER_ID)

    channel = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")

    subtype = event.get("subtype")

    raw_text = (event.get("text") or "").strip()
    lower_text = raw_text.lower()

    if f"<@{BOT_USER_ID}>" in raw_text and lower_text.endswith("delete"):
        remaining = delete_all_embeddings()
        client.chat_postMessage(
            channel=event["channel"],
            text=f"✅ Deleted all embeddings."
        )
        return

    if (
        lower_text.startswith("issue")
        or lower_text.startswith("@BoltApp issue")
        or (f"<@{BOT_USER_ID}>" in raw_text and lower_text.startswith(f"<@{BOT_USER_ID}> issue"))
    ):
        if lower_text.startswith("@BoltApp issue"):
            cmd_text = raw_text[len("@BoltApp"):].strip()
        else:
            cmd_text = raw_text.replace(f"<@{BOT_USER_ID}>", "", 1).strip()
        issue_payload = cmd_text[len("issue"):].strip()

        title, body = "", ""
        if "|" in issue_payload:
            title_part, body_part = issue_payload.split("|", 1)
            title, body = title_part.strip(), body_part.strip()
        else:
            title = issue_payload.strip()

        if not title:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="❌ Usage: `<@bot> issue Title | optional body`",
            )
            return

        if not body:
            body = f"Created via Slack by <@{event['user']}> in channel {channel}."

        try:
            number, url = create_github_issue(title=title, body=body)
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"✅ Created GitHub issue #{number}: {url}",
            )
        except Exception as e:
            logger.exception("Failed creating GitHub issue")
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"❌ Failed to create issue: {e}",
            )
        return

     # ---- A) File upload case (subtype=file_share) ----
    if subtype == "file_share":
        for f in event.get("files", []):
            file_id = f.get("id")
            if not file_id:
                continue

            if file_id in INDEXED_FILE_IDS:
                continue

            info = client.files_info(file=file_id)
            file_obj = info["file"]

            url = file_obj.get("url_private_download") or file_obj.get("url_private")
            name = file_obj.get("name") or file_obj.get("title") or f"{file_id}.bin"
            dest = SAVE_DIR / f"{file_id}-{name}"

            if not url:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"❌ Could not find a download URL for `{name}`.",
                )
                continue

            ext = pathlib.Path(name).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"❌ Unsupported file type `{ext or name}`. Only .pdf and .docx are allowed.",
                )
                continue

            try:
                data = download_slack_file(url)
                dest.write_bytes(data)

                user_id = event['user']
                result = client.users_info(user=user_id)
                user_name = result['user']['profile']['display_name'] or result['user']['real_name']

                # Index into RAG
                ids = index_slack_file_bytes(file_bytes=data, file_obj=file_obj, user_id=user_name, channel_id=channel)

                INDEXED_FILE_IDS.add(file_id)

                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"✅ Saved `{name}` and indexed {len(ids)} chunks.",
                )
            except Exception as e:
                logger.exception("Failed processing uploaded file")
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"❌ Failed to process `{name}`: {e}",
                )
            return
    
    
    if (event.get("type") == "message" and not event.get("subtype") and bool(event.get("text"))):
        text = (event.get("text") or "").strip()
        if not text:
            return

        try:
            answer = answer_query(text, slack_channel=channel, k=4)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)
        except Exception as e:
            logger.exception("Failed answering query")
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"❌ Error answering: {e}",
        )


# Start your app
if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
