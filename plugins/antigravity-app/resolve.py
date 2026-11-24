"""Prints the latest Antigravity release for this processor as JSON.

Google publishes the Linux app only as versioned archives; its download page
reads the list of releases from this service. No checksum is published."""
import json
import re
import sys
import urllib.request

RELEASES = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases"
FOLDERS = {"amd64": "linux-x64", "arm64": "linux-arm"}


def key(release):
    return tuple(int(part) for part in release["version"].split("."))


def main():
    folder = FOLDERS[sys.argv[1]]
    with urllib.request.urlopen(RELEASES, timeout=30) as response:
        releases = json.load(response)
    valid = [r for r in releases if isinstance(r, dict)
             and re.fullmatch(r"\d+(\.\d+){1,3}", str(r.get("version", "")))
             and re.fullmatch(r"\d{1,24}", str(r.get("execution_id", "")))]
    if not valid:
        sys.exit("no release listed")
    latest = max(valid, key=key)
    url = ("https://storage.googleapis.com/antigravity-public/antigravity-hub/"
           f"{latest['version']}-{latest['execution_id']}/{folder}/Antigravity.tar.gz")
    print(json.dumps({"version": latest["version"], "url": url}))


main()
