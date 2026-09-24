"""Run locally once to create a refresh token; never commit its output."""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

data = json.loads(input("Paste OAuth client JSON: "))
scopes = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
flow = InstalledAppFlow.from_client_config(data, scopes)
credentials = flow.run_local_server(port=0)
print("YOUTUBE_CLIENT_ID=", credentials.client_id)
print("YOUTUBE_CLIENT_SECRET=", credentials.client_secret)
print("YOUTUBE_REFRESH_TOKEN=", credentials.refresh_token)

