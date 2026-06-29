from surogates.channels.platforms.slack import parse
from surogates.channels.inbound import InboundFileRef


def _event(files):
    return {
        "type": "event_callback",
        "event": {
            "type": "message", "channel": "C1", "user": "U1",
            "text": "see attached", "ts": "100.0", "files": files,
        },
    }


def test_parse_populates_files_from_slack_files():
    body = _event([
        {"url_private_download": "https://files.slack.com/d1", "url_private": "https://files.slack.com/p1",
         "name": "report.pdf", "mimetype": "application/pdf", "size": 1234},
        {"url_private": "https://files.slack.com/p2", "name": "pic.png", "mimetype": "image/png", "size": 50},
    ])
    msg = parse(body, bot_user_id="U_BOT")
    assert msg is not None
    assert msg.files == [
        InboundFileRef(url="https://files.slack.com/d1", filename="report.pdf",
                       mime_type="application/pdf", size=1234),
        InboundFileRef(url="https://files.slack.com/p2", filename="pic.png",
                       mime_type="image/png", size=50),
    ]
    # compatibility: media_urls + text marker still populated
    assert msg.media_urls == ["https://files.slack.com/d1", "https://files.slack.com/p2"]
    assert "[shared 2 file(s)" in msg.text


def test_parse_guesses_mime_when_slack_omits_it():
    body = _event([{"url_private": "https://files.slack.com/p3", "name": "notes.txt"}])
    msg = parse(body, bot_user_id="U_BOT")
    assert msg.files[0].mime_type == "text/plain"   # mimetypes.guess_type(".txt")


def test_parse_no_files_means_empty_files_list():
    body = _event([])
    msg = parse(body, bot_user_id="U_BOT")
    assert msg.files == []
