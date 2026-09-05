"""Application-wide API settings distributed with Sub-Zero's mobile app."""

# The API also requires the signed-in owner's bearer token and user identifier.
SUBSCRIPTION_KEY = "e88bf0b60baf441583f822fa9ba9c895"

# Android app 4.6.2 bundles Dart 3.11.5; dart:io uses the major/minor version.
APP_HEADERS = {
    "User-Agent": "Dart/3.11 (dart:io)",
    "Accept-Encoding": "gzip",
}

# AppAuth uses Android's native HTTP client for tokens and a browser for sign-in.
# These are representative Android 16 / Chrome 153 profiles, not a traffic capture.
AUTH_HEADERS = {
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 16; Pixel 9 Build/BP2A.250605.031.A2)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
}
LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
