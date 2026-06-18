import requests
import urllib3
from urllib.parse import parse_qs
from bs4 import BeautifulSoup
from PIL import Image
import io
import json
import os
import sys
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Configuration ────────────────────────────────────────────────────────────
# Credentials read from environment variables (set as GitHub Secrets in CI,
# or as real env vars locally — never hardcoded here)
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL",   "bradsdogwatcher@gmail.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD",  "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL",  SENDER_EMAIL)

URL      = ("https://petharbor.com/results.asp?searchtype=ADOPT&start=4&miles=20"
            "&shelterlist=%27CARR%27&zip=&where=type_DOG&friends=0&rows&nosuccess=1"
            "&nomax=1&rows=100&nobreedreq=1&nopod=1&nocustom=1&imgres=detail")
BASE_URL = "https://petharbor.com/"

_HERE           = os.path.dirname(os.path.abspath(__file__))
DATA_FILE       = os.path.join(_HERE, "dog_history.json")
IMAGES_DIR      = os.path.join(_HERE, "DogImages")
EMAIL_LIST_FILE = os.path.join(_HERE, "email_list.txt")

IMG_MAX_W = 240
IMG_MAX_H = 320

# Realistic browser headers help avoid blocks on cloud IPs
HEADERS = {
    "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
    "Accept":          ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control":   "max-age=0",
}


# ── Fetch ────────────────────────────────────────────────────────────────────

def get_current_dogs(max_retries=3, retry_delay=10):
    for attempt in range(1, max_retries + 1):
        try:
            print(f"Fetching petharbor.com (attempt {attempt}/{max_retries})...")
            response = requests.get(URL, headers=HEADERS, timeout=30, verify=False)
            print(f"  HTTP {response.status_code}  |  {len(response.content):,} bytes received")

            if response.status_code == 403:
                print("  403 Forbidden -- the site may be blocking this IP range.")
                break
            if response.status_code != 200:
                print(f"  Unexpected status {response.status_code}; retrying...")
                time.sleep(retry_delay)
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            current_dogs = {}
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if "detail.asp" not in href.lower():
                    continue
                qs     = parse_qs(href.split("?", 1)[-1] if "?" in href else "")
                dog_id = next((v[0] for k, v in qs.items() if k.upper() == "ID"), None)
                if not dog_id:
                    continue
                row = link.find_parent("tr")
                if not row:
                    continue
                cells = row.find_all("td")
                if len(cells) < 7:
                    continue
                img_tag = link.find("img")
                img_src = (img_tag["src"] if img_tag and img_tag.get("src")
                           else f"get_image.asp?RES=Detail&ID={dog_id}&LOCATION=CARR")
                current_dogs[dog_id] = {
                    "name":    cells[2].text.strip(),
                    "gender":  cells[3].text.strip(),
                    "color":   cells[4].text.strip(),
                    "breed":   cells[5].text.strip(),
                    "age":     cells[6].text.strip(),
                    "img_url": BASE_URL + img_src,
                }

            print(f"  Parsed {len(current_dogs)} dogs from page.")
            if not current_dogs:
                print("  WARNING: page returned 200 but no dogs were parsed --"
                      " the site's HTML structure may have changed.")
            return current_dogs

        except requests.exceptions.Timeout:
            print(f"  Request timed out (attempt {attempt}).")
        except Exception as e:
            print(f"  Error on attempt {attempt}: {e}")

        if attempt < max_retries:
            print(f"  Retrying in {retry_delay}s...")
            time.sleep(retry_delay)

    return None


# ── Persistence ──────────────────────────────────────────────────────────────

def load_previous_dogs():
    if not os.path.exists(DATA_FILE):
        return {}, None
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
    last_run = data.pop("__last_run__", None)
    result = {}
    for dog_id, val in data.items():
        if isinstance(val, str):
            result[dog_id] = {"name": val, "gender": "", "color": "", "breed": "", "age": ""}
        else:
            result[dog_id] = val
    return result, last_run


def save_current_dogs(dogs):
    from datetime import datetime
    history = {
        dog_id: {k: v for k, v in info.items() if k != "img_url"}
        for dog_id, info in dogs.items()
    }
    history["__last_run__"] = datetime.now().strftime("%B %d, %Y %I:%M %p")
    with open(DATA_FILE, "w") as f:
        json.dump(history, f, indent=4)


# ── Image helpers ─────────────────────────────────────────────────────────────

def _image_path(dog_id):
    return os.path.join(IMAGES_DIR, f"{dog_id}.jpg")


def cache_image(dog_id, img_url):
    if os.path.exists(IMAGES_DIR) and not os.path.isdir(IMAGES_DIR):
        print(f"WARNING: {IMAGES_DIR} exists as a file — removing it.")
        os.remove(IMAGES_DIR)
    os.makedirs(IMAGES_DIR, exist_ok=True)
    path = _image_path(dog_id)
    if os.path.exists(path):
        return path
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=10, verify=False)
        if r.status_code == 200 and r.content:
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception:
        pass
    return None


def remove_cached_image(dog_id):
    path = _image_path(dog_id)
    if os.path.exists(path):
        os.remove(path)


def _resize_bytes(raw_bytes, max_w=IMG_MAX_W, max_h=IMG_MAX_H):
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def load_image_bytes(dog_id, img_url=None):
    raw  = None
    path = _image_path(dog_id)
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = f.read()
    elif img_url:
        try:
            r = requests.get(img_url, headers=HEADERS, timeout=10, verify=False)
            if r.status_code == 200 and r.content:
                raw = r.content
        except Exception:
            pass
    if raw:
        try:
            return _resize_bytes(raw)
        except Exception:
            return raw
    return None


# ── Email ─────────────────────────────────────────────────────────────────────

def load_email_list():
    if not os.path.exists(EMAIL_LIST_FILE):
        return []
    with open(EMAIL_LIST_FILE, "r") as f:
        return [line.strip() for line in f if line.strip()]


def send_email(subject, html_body, images=None):
    if not EMAIL_PASSWORD:
        print("ERROR: EMAIL_PASSWORD is not set -- skipping email.")
        return

    cc_list = load_email_list()
    msg = MIMEMultipart("related")
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECEIVER_EMAIL
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(html_body, "html"))

    for cid, img_bytes in (images or []):
        part = MIMEImage(img_bytes)
        part.add_header("Content-ID", f"<{cid}>")
        part.add_header("Content-Disposition", "inline")
        msg.attach(part)

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(SENDER_EMAIL, EMAIL_PASSWORD)
        all_recipients = list({RECEIVER_EMAIL} | set(cc_list))
        server.sendmail(SENDER_EMAIL, all_recipients, msg.as_string())
        server.quit()
        print(f"Email sent to {all_recipients}")
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")
        sys.exit(1)


# ── HTML card builder ─────────────────────────────────────────────────────────

def dog_card_html(dog_id, info, cid_prefix, img_url=None, bordered=True):
    cid        = f"{cid_prefix}_{dog_id}"
    img_bytes  = load_image_bytes(dog_id, img_url or info.get("img_url"))
    detail_url = f"{BASE_URL}pet.asp?uaid=CARR.{dog_id}"
    img_html   = (
        f'<a href="{detail_url}" target="_blank">'
        f'<img src="cid:{cid}" style="display:block;margin:6px 0;"></a>'
        if img_bytes else
        f'<a href="{detail_url}" target="_blank"><em>(no photo -- click to view)</em></a>'
    )
    rows = "".join(
        f'<tr><td style="color:#666;padding-right:10px;"><b>{label}</b></td>'
        f'<td>{info.get(key, "") or "&mdash;"}</td></tr>'
        for label, key in [("Gender", "gender"), ("Color", "color"),
                            ("Breed", "breed"),  ("Age",   "age")]
    )
    div_style = ("margin-bottom:20px;padding-bottom:16px;border-bottom:1px solid #ddd;"
                 if bordered else "")
    card = f"""
    <div style="{div_style}">
        <strong style="font-size:1.1em;">{info['name']}</strong>
        <span style="color:#888;font-size:0.85em;"> &mdash; ID: {dog_id}</span><br>
        <table style="margin:4px 0;font-size:0.9em;border-collapse:collapse;">{rows}</table>
        {img_html}
    </div>"""
    return card, (cid, img_bytes) if img_bytes else None


# ── Main report ───────────────────────────────────────────────────────────────

def generate_report():
    current = get_current_dogs()
    if current is None:
        print("ERROR: Could not fetch page data after all retries -- aborting.")
        sys.exit(1)
    if not current:
        print("ERROR: Page returned no dogs -- possible site change or IP block -- aborting.")
        sys.exit(1)

    previous, last_run = load_previous_dogs()
    is_baseline = not previous
    since       = f" Since {last_run}" if last_run else ""

    current_ids  = set(current.keys())
    previous_ids = set(previous.keys())
    added_ids    = set() if is_baseline else current_ids - previous_ids
    removed_ids  = set() if is_baseline else previous_ids - current_ids

    for dog_id, info in current.items():
        if is_baseline or dog_id in added_ids:
            cache_image(dog_id, info["img_url"])

    # ── Build email ──────────────────────────────────────────────────────────
    if is_baseline:
        subject = "PetHarbor Tracker: Initialized"
        header  = (f"<h2>PetHarbor Tracker: Initialized</h2>"
                   f"<p>Tracking started. Baseline set with <strong>{len(current)}</strong> dogs.</p>")
    elif added_ids or removed_ids:
        subject = f"PetHarbor Update: Changes Detected ({len(added_ids)} added, {len(removed_ids)} removed)"
        header  = f"<h2>PetHarbor Update: Changes Detected{since}</h2>"
    else:
        subject = "PetHarbor Update: No Changes Today"
        header  = f"<h2>PetHarbor Update: No Changes Today{since}</h2>"

    html   = header
    images = []

    def add_card(dog_id, info, cid_prefix, img_url=None, bordered=True):
        card, img_pair = dog_card_html(dog_id, info, cid_prefix, img_url=img_url, bordered=bordered)
        if img_pair:
            images.append(img_pair)
        return card

    DIVIDER = ("<p>&nbsp;</p>"
               "<hr style='border:none;border-top:2px solid #ccc;'>"
               "<p>&nbsp;</p>")

    html += "<h2 style='color:#a03030;font-size:1.5em;'>Dogs Removed</h2>"
    if removed_ids:
        for dog_id in removed_ids:
            fallback = f"{BASE_URL}get_image.asp?RES=Detail&ID={dog_id}&LOCATION=CARR"
            html += add_card(dog_id, previous[dog_id], "removed", img_url=fallback)
    else:
        msg = ("First run -- no prior data." if is_baseline
               else "No dogs were removed since the last check.")
        html += f"<p><em>{msg}</em></p>"

    html += DIVIDER

    html += "<h2 style='color:#2a7a2a;font-size:1.5em;'>Dogs Added</h2>"
    if added_ids:
        for dog_id in added_ids:
            html += add_card(dog_id, current[dog_id], "added")
    else:
        msg = ("First run -- all dogs below are the starting baseline." if is_baseline
               else "No new dogs were added since the last check.")
        html += f"<p><em>{msg}</em></p>"

    html += DIVIDER

    html += f"<h2 style='color:#333;font-size:1.5em;'>Full Current List ({len(current)} dogs)</h2>"
    html += "<table style='border-collapse:collapse;width:100%;'>"
    for i, (dog_id, info) in enumerate(current.items()):
        if i % 3 == 0:
            html += "<tr>"
        cell_card = add_card(dog_id, info, "current", bordered=False)
        html += (f"<td style='vertical-align:top;padding:8px;width:33%;border:1px solid #999;'>"
                 f"{cell_card}</td>")
        if i % 3 == 2:
            html += "</tr>"
    remainder = len(current) % 3
    if remainder:
        html += "".join("<td style='width:33%;'></td>" for _ in range(3 - remainder))
        html += "</tr>"
    html += "</table>"

    send_email(subject, html, images)

    for dog_id in removed_ids:
        remove_cached_image(dog_id)

    save_current_dogs(current)

    if is_baseline:
        print(f"Baseline initialized with {len(current)} dogs.")
    elif added_ids or removed_ids:
        print(f"Changes: {len(added_ids)} added, {len(removed_ids)} removed.")
    else:
        print("No changes detected.")


if __name__ == "__main__":
    generate_report()
