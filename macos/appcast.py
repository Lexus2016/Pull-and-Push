"""Write the Sparkle appcast (appcast.xml) for one release.

Every GitHub release carries its own appcast.xml; the app reads
releases/latest/download/appcast.xml, so the feed always names the newest release, while the
enclosure URL is pinned to that release's tag (a newer release cannot swap the file under a client
that is mid-download). release.sh then signs the whole feed with sign_update.

    python3 appcast.py --version 0.4.0 --build 400 --repo owner/name --dmg path.dmg \
        --ed-signature SIG --notes-html notes.html --out appcast.xml
"""

from __future__ import annotations

import argparse
import os
from email.utils import formatdate
from xml.sax.saxutils import escape, quoteattr


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("--version", required=True)
    a.add_argument("--build", required=True)
    a.add_argument("--repo", required=True)
    a.add_argument("--dmg", required=True)
    a.add_argument("--ed-signature", required=True)
    a.add_argument("--notes-html", required=True)
    a.add_argument("--min-macos", default="13.0")
    a.add_argument("--out", required=True)
    o = a.parse_args()
    tag = f"v{o.version}"
    name = os.path.basename(o.dmg)
    url = f"https://github.com/{o.repo}/releases/download/{tag}/{name}"
    notes = open(o.notes_html, encoding="utf-8").read().replace("]]>", "]]&gt;")
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Pull-and-Push</title>
    <link>https://github.com/{escape(o.repo)}</link>
    <description>Pull-and-Push updates</description>
    <item>
      <title>Pull-and-Push {escape(o.version)}</title>
      <pubDate>{formatdate(usegmt=True)}</pubDate>
      <sparkle:version>{escape(o.build)}</sparkle:version>
      <sparkle:shortVersionString>{escape(o.version)}</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>{escape(o.min_macos)}</sparkle:minimumSystemVersion>
      <sparkle:fullReleaseNotesLink>https://github.com/{escape(o.repo)}/releases/tag/{escape(tag)}</sparkle:fullReleaseNotesLink>
      <description><![CDATA[{notes}]]></description>
      <enclosure url={quoteattr(url)} length="{os.path.getsize(o.dmg)}" type="application/octet-stream"
                 sparkle:edSignature={quoteattr(o.ed_signature)}/>
    </item>
  </channel>
</rss>
"""
    with open(o.out, "w", encoding="utf-8") as f:
        f.write(xml)


if __name__ == "__main__":
    main()
