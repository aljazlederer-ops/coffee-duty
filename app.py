import os
os.makedirs("instance", exist_ok=True)

import random
import json
import base64
from datetime import datetime, timedelta
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request,
    redirect, url_for, flash
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_

import requests
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest

# --------------------------------------------------
# KONFIGURACIJA APLIKACIJE
# --------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "coffee_duty.db")
SCHEDULER_TOKEN = os.environ.get("SCHEDULER_TOKEN")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --------------------------------------------------
# INITIALIZE DATABASE (Flask 3 compatible)
# --------------------------------------------------
with app.app_context():
    db.create_all()
    
# Gmail OAuth ENV spremenljivke
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REDIRECT_URI = os.environ.get("GMAIL_REDIRECT_URI")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# --------------------------------------------------
# MODELI
# --------------------------------------------------
class CoffeeType(db.Model):
    __tablename__ = "coffee_types"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(100), nullable=True)
    active = db.Column(db.Boolean, default=True)

    people = db.relationship("Person", back_populates="default_coffee_type")

class Person(db.Model):
    __tablename__ = "people"
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=True)   # opcijski
    email = db.Column(db.String(200), nullable=True)       # opcijski
    default_coffee_type_id = db.Column(
        db.Integer, db.ForeignKey("coffee_types.id"),
        nullable=True
    )
    is_present = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)

    default_coffee_type = db.relationship("CoffeeType", back_populates="people")

    selections = db.relationship(
        "Selection",
        back_populates="person",
        cascade="all, delete-orphan"
    )

class Selection(db.Model):
    __tablename__ = "selections"
    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("people.id"), nullable=False)
    selected_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(50), default="manual")  # manual / auto
    slot = db.Column(db.String(20), nullable=True)       # morning / afternoon / manual
    email_subject = db.Column(db.Text, nullable=True)
    email_body = db.Column(db.Text, nullable=True)

    person = db.relationship("Person", back_populates="selections")


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


# --------------------------------------------------
# NASTAVITVE
# --------------------------------------------------
def get_setting(key: str):
    row = Setting.query.get(key)
    return row.value if row else None

def set_setting(key: str, value: str | None):
    row = Setting.query.get(key)
    if not row:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()

def is_automation_enabled() -> bool:
    return get_setting("automation_enabled") == "1"

def is_admin():
    return request.args.get("admin") == ADMIN_TOKEN

# --------------------------------------------------
# Gmail CREDENTIALS – SHRANJEVANJE / BRANJE
# --------------------------------------------------
def _get_gmail_credentials() -> Credentials | None:
    token_json = get_setting("gmail_token")
    if not token_json:
        return None
    data = json.loads(token_json)
    creds = Credentials(
        token=data["token"],
        refresh_token=data.get("refresh_token"),
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data["scopes"],
    )
    return creds


def _save_gmail_credentials(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    set_setting("gmail_token", json.dumps(data))


def is_gmail_connected() -> bool:
    return _get_gmail_credentials() is not None


# --------------------------------------------------
# Gmail OAuth – URL, CALLBACK, DISCONNECT
# --------------------------------------------------
def _build_gmail_auth_url() -> str:
    if not GMAIL_CLIENT_ID or not GMAIL_REDIRECT_URI:
        raise RuntimeError("GMAIL_CLIENT_ID ali GMAIL_REDIRECT_URI nista nastavljena v environmentu.")

    params = {
        "client_id": GMAIL_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


@app.route("/authorize-gmail")
def authorize_gmail():
    try:
        url = _build_gmail_auth_url()
    except RuntimeError as e:
        flash(str(e), "danger")
        return redirect(url_for("index"))
    return redirect(url)


@app.route("/oauth2callback")
def oauth2callback():
    error = request.args.get("error")
    if error:
        flash(f"Gmail autorizacija zavrnjena: {error}", "danger")
        return redirect(url_for("index"))

    code = request.args.get("code")
    if not code:
        flash("Manjka 'code' iz Google OAuth.", "danger")
        return redirect(url_for("index"))

    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET or not GMAIL_REDIRECT_URI:
        flash("Gmail OAuth env spremenljivke niso nastavljene.", "danger")
        return redirect(url_for("index"))

    token_endpoint = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": GMAIL_CLIENT_ID,
        "client_secret": GMAIL_CLIENT_SECRET,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "grant_type": "authorization_code",
    }

    resp = requests.post(token_endpoint, data=data)
    if resp.status_code != 200:
        flash(f"Napaka pri pridobivanju tokena: {resp.text}", "danger")
        return redirect(url_for("index"))

    token_data = resp.json()
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        flash("Google ni vrnil access tokena.", "danger")
        return redirect(url_for("index"))

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=token_endpoint,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    _save_gmail_credentials(creds)

    flash("Gmail povezava uspešno vzpostavljena. ✅", "success")
    return redirect(url_for("index"))


@app.route("/gmail/disconnect", methods=["POST"])
def gmail_disconnect():
    set_setting("gmail_token", None)
    flash("Gmail povezava je bila odstranjena.", "info")
    return redirect(url_for("index"))


# --------------------------------------------------
# POŠILJANJE EMAILA PREKO GMAIL API
# --------------------------------------------------
def send_email(to_email: str, subject: str, body: str):
    """Pošlje e-mail preko Gmail API, če je povezava vzpostavljena."""
    creds = _get_gmail_credentials()
    if not creds:
        print("Ni Gmail credentials – email ne bo poslan.")
        return

    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        _save_gmail_credentials(creds)

    service = build("gmail", "v1", credentials=creds)
    message = MIMEText(body, _charset="utf-8")
    message["to"] = to_email
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# --------------------------------------------------
# EMAIL VSEBINA – skupna logika
# --------------------------------------------------
def build_email_content(sel: Selection, slot: str):
    """Vrne (subject, body) glede na izbran termin in izbranega dežurnega."""
    now = datetime.now()

    if slot == "morning":
        next_dt = now.replace(hour=13, minute=15, second=0, microsecond=0)
        next_label = "13:15"
        next_date_str = next_dt.strftime("%d.%m.%Y")

        body = f"""Pozdravljeni,

Za jutranji termin (8:30) je dežurni:
- {sel.person.first_name} {sel.person.last_name}.

Naslednji žreb bo {next_date_str} ob {next_label}.

Lep pozdrav,
Sistem za dežurstvo kuhanja kave ☕"""
        subject = "Dežurni za jutranji termin"

    elif slot == "afternoon":
        next_dt = now + timedelta(days=1)
        while next_dt.weekday() >= 5:  # sobota/nedelja
            next_dt += timedelta(days=1)
        next_dt = next_dt.replace(hour=8, minute=15, second=0, microsecond=0)
        next_label = "08:15"
        next_date_str = next_dt.strftime("%d.%m.%Y")

        body = f"""Pozdravljeni,

Za popoldanski termin (13:30) je dežurni:
- {sel.person.first_name} {sel.person.last_name}.

Naslednji žreb bo {next_date_str} ob {next_label}.

Lep pozdrav,
Sistem za dežurstvo kuhanja kave ☕"""
        subject = "Dežurni za popoldanski termin"

    else:
        body = f"""Pozdravljeni,

Dežurni za kuhanje kave je:
- {sel.person.first_name} {sel.person.last_name}.

Lep pozdrav,
Sistem za dežurstvo kuhanja kave ☕"""
        subject = "Dežurni za kavo"

    return subject, body

# --------------------------------------------------
# STATISTIKA OSEB + UTEŽI  (POPOLNOMA POPRAVLJENO)
# --------------------------------------------------
def compute_person_stats(only_present: bool = False):
    """
    Vrne listo slovarjev:
    {
      "person": Person,
      "name": "Ime Priimek",
      "total": št_dežurstev (samo AUTO),
      "last_date": datetime ali None (samo AUTO),
      "days_since": int,
      "weight": float,
      "prob": float (0-100)
    }
    """

    now = datetime.now()
    query = Person.query.filter_by(active=True)
    if only_present:
        query = query.filter_by(is_present=True)

    persons = query.all()
    stats = []

    for p in persons:

        # ✔ TOTAL = samo avtomatske izbire
        total = (
            Selection.query
            .filter_by(person_id=p.id, source="auto")
            .count()
        )

        # ✔ LAST = zadnja avtomatska izbira
        last_sel = (
            Selection.query
            .filter_by(person_id=p.id, source="auto")
            .order_by(Selection.selected_at.desc())
            .first()
        )

        if last_sel:
            days = (now - last_sel.selected_at).days
            last_dt = last_sel.selected_at
        else:
            days = 10   # boost za osebe brez avtomatske izbire
            last_dt = None

        # ✔ formula uteži: temelji SAMO na avtomatskih podatkih
        weight = (days + 1) / (total + 1)

        stats.append({
            "person": p,
            "name": f"{p.first_name} {p.last_name or ''}".strip(),
            "total": total,
            "last_date": last_dt,
            "days_since": days if last_dt else 0,
            "weight": weight,
            "prob": 0.0,
        })

    # izračun verjetnosti
    total_weight = sum(s["weight"] for s in stats)
    if total_weight > 0:
        for s in stats:
            s["prob"] = round(s["weight"] / total_weight * 100, 1)
    else:
        for s in stats:
            s["prob"] = 0.0

    return stats

def weighted_random_choice(stats):
    """Izbere osebo glede na uteži."""
    # odstranimo tiste z 0 uteži
    stats = [s for s in stats if s["weight"] > 0]
    if not stats:
        return None

    total = sum(s["weight"] for s in stats)
    r = random.uniform(0, total)
    upto = 0.0

    for s in stats:
        if upto + s["weight"] >= r:
            return s["person"]
        upto += s["weight"]

    # fallback (ne bi se smelo zgoditi)
    return stats[-1]["person"]

def run_auto_selection():
    """Izvede avtomatski žreb za trenutni termin (jutranji/popoldanski)."""

    if not is_automation_enabled():
        return False, "Avtomatika je izklopljena."

    # prisotni + aktivni
    stats = compute_person_stats(only_present=True)
    if not stats:
        return False, "Ni prisotnih oseb za avtomatski žreb."

    person = weighted_random_choice(stats)
    if not person:
        return False, "Ni mogoče izvesti avtomatskega žreba."

    now = datetime.now()
    # zelo enostavna logika za slot:
    slot = "morning" if now.hour < 12 else "afternoon"

    # zapišemo izbor kot AUTO
    sel = Selection(
        person_id=person.id,
        source="auto",
        slot=slot
    )
    db.session.add(sel)
    db.session.commit()

    # pošljemo e-mail, če oseba ima email
    if person.email:
        subject, body = build_email_content(sel, slot)
        send_email(person.email, subject, body)
        sel.email_subject = subject
        sel.email_body = body
        db.session.commit()

    return True, f"Izbran je bil {person.first_name} {person.last_name or ''} (AUTO, {slot})."

# --------------------------------------------------
# IZRAČUN NASLEDNJEGA ŽREBA (za prikaz)
# --------------------------------------------------
def compute_next_auto_run_dynamic():
    """Vrne naslednji termin avtomatskega žreba glede na trenutni čas."""
    now = datetime.now()

    today_morning = now.replace(hour=8, minute=15, second=0, microsecond=0)
    today_afternoon = now.replace(hour=13, minute=15, second=0, microsecond=0)

    if now < today_morning:
        return today_morning

    if now < today_afternoon:
        return today_afternoon

    next_day = now + timedelta(days=1)
    while next_day.weekday() >= 5:  # sobota, nedelja
        next_day += timedelta(days=1)

    return next_day.replace(hour=8, minute=15, second=0, microsecond=0)


# --------------------------------------------------
# INDEX
# --------------------------------------------------
@app.route("/")
def index():
    people = Person.query.order_by(Person.first_name).all()
    coffee_types = CoffeeType.query.order_by(CoffeeType.name).all()
    last_selection = Selection.query.order_by(Selection.selected_at.desc()).first()

    present_count = Person.query.filter_by(is_present=True, active=True).count()

    # Najbolj dejaven (po številu dežurstev)
    duty_counts_query = (
        db.session.query(
            Person.first_name,
            Person.last_name,
            db.func.count(Selection.id)
        )
        .join(Selection, Selection.person_id == Person.id)
        .filter(Selection.source == "auto")  # ✔ samo avtomatika
        .group_by(Person.id)
        .order_by(db.func.count(Selection.id).desc())
        .all()
    )

    best_person = None
    if duty_counts_query:
        top_first, top_last, _ = duty_counts_query[0]
        best_person = (
            Person.query
            .filter_by(first_name=top_first, last_name=top_last)
            .first()
        )

    # Priljubljena kava – po številu ljudi, ki jo imajo kot privzeto
    favorite_coffee = (
        db.session.query(
            CoffeeType,
            db.func.count(Person.id).label("cnt")
        )
        .join(Person, Person.default_coffee_type_id == CoffeeType.id)
        .group_by(CoffeeType.id)
        .order_by(db.desc("cnt"))
        .first()
    )
    favorite_coffee = favorite_coffee[0] if favorite_coffee else None

    next_auto_run = compute_next_auto_run_dynamic()
    automation = is_automation_enabled()
    gmail_connected = is_gmail_connected()

    # STATISTIKA ZA GRAF + TABELO
    person_stats = compute_person_stats(only_present=False)
    chart_labels = [s["name"] for s in person_stats]
    chart_counts = [s["total"] for s in person_stats]
    chart_days = [s["days_since"] for s in person_stats]
    chart_weights = [round(s["weight"], 3) for s in person_stats]

    return render_template(
        "index.html",
        people=people,
        coffee_types=coffee_types,
        last_selection=last_selection,
        automation_enabled=automation,
        present_count=present_count,
        best_person=best_person,
        favorite_coffee=favorite_coffee,
        chart_labels=chart_labels,
        chart_counts=chart_counts,
        chart_days=chart_days,
        chart_weights=chart_weights,
        person_stats=person_stats,
        next_auto_run=next_auto_run,
        gmail_connected=gmail_connected,
        is_admin=is_admin(),
    )


# --------------------------------------------------
# PEOPLE – LIST + CRUD
# --------------------------------------------------
@app.route("/people")
def people_list():
    q = request.args.get("q", "").strip()
    query = Person.query.order_by(Person.first_name)

    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Person.first_name.ilike(like),
                Person.last_name.ilike(like),
                Person.email.ilike(like),
            )
        )

    people = query.all()
    coffee_types = CoffeeType.query.order_by(CoffeeType.name).all()

    return render_template(
        "people.html",
        people=people,
        coffee_types=coffee_types,
        q=q
    )


@app.route("/people/add", methods=["POST"])
def people_add():
    first = request.form.get("first_name")
    last = request.form.get("last_name")
    email = request.form.get("email")
    default_ct = request.form.get("default_coffee_type_id") or None
    is_present = bool(request.form.get("is_present"))

    p = Person(
        first_name=first,
        last_name=last,
        email=email,
        default_coffee_type_id=default_ct,
        is_present=is_present,
        active=True
    )
    db.session.add(p)
    db.session.commit()
    flash("Oseba dodana.", "success")
    return redirect(url_for("people_list"))


@app.route("/people/edit/<int:person_id>", methods=["POST"])
def people_edit(person_id):
    p = Person.query.get_or_404(person_id)

    p.first_name = request.form.get("first_name")
    p.last_name = request.form.get("last_name")
    p.email = request.form.get("email")
    default_ct = request.form.get("default_coffee_type_id") or None
    p.default_coffee_type_id = default_ct
    p.is_present = bool(request.form.get("is_present"))

    db.session.commit()
    flash("Oseba posodobljena.", "success")
    return redirect(url_for("people_list"))


@app.route("/people/delete/<int:person_id>", methods=["POST"])
def people_delete(person_id):
    p = Person.query.get_or_404(person_id)
    db.session.delete(p)
    db.session.commit()
    flash("Oseba izbrisana.", "info")
    return redirect(url_for("people_list"))


# --------------------------------------------------
# COFFEE TYPES – LIST + CRUD
# --------------------------------------------------
@app.route("/coffee_types")
@app.route("/coffee-types")
def coffee_types_list():
    coffee_types = CoffeeType.query.order_by(CoffeeType.name).all()
    return render_template("coffee_types.html", coffee_types=coffee_types)


@app.route("/coffee_types_list")
def coffee_types_list_alias():
    return redirect(url_for("coffee_types_list"))


@app.route("/coffee_types/add", methods=["POST"])
def coffee_types_add():
    name = request.form.get("name")
    icon = request.form.get("icon") or None

    ct = CoffeeType(name=name, icon=icon, active=True)
    db.session.add(ct)
    db.session.commit()
    flash("Tip kave dodan.", "success")
    return redirect(url_for("coffee_types_list"))


@app.route("/coffee_types/edit/<int:ct_id>", methods=["POST"])
def coffee_types_edit(ct_id):
    ct = CoffeeType.query.get_or_404(ct_id)
    ct.name = request.form.get("name")
    ct.icon = request.form.get("icon") or None
    db.session.commit()
    flash("Tip kave posodobljen.", "success")
    return redirect(url_for("coffee_types_list"))


@app.route("/coffee_types/delete/<int:ct_id>", methods=["POST"])
def coffee_types_delete(ct_id):
    ct = CoffeeType.query.get_or_404(ct_id)
    db.session.delete(ct)
    db.session.commit()
    flash("Tip kave izbrisan.", "info")
    return redirect(url_for("coffee_types_list"))


# --------------------------------------------------
# RANDOM IZBIRA – Z UTEŽMI
# --------------------------------------------------
@app.route("/random")
def random_selection():
    if is_automation_enabled():
        return {"error": "Avtomatika je vključena – ročna izbira ni dovoljena."}

    stats = compute_person_stats(only_present=True)
    if not stats:
        return {"error": "Ni prisotnih oseb za izbor."}

    person = weighted_random_choice(stats)
    if not person:
        return {"error": "Ni mogoče izbrati osebe."}

    sel = Selection(
        person_id=person.id,
        source="manual",
        slot=None
    )
    db.session.add(sel)
    db.session.commit()

    return {
        "person_id": person.id,
        "person_name": f"{person.first_name} {person.last_name}",
        "selection_id": sel.id
    }

@app.route("/run-auto")
def run_auto():
    token_admin = request.args.get("admin")
    token_scheduler = request.args.get("scheduler")

    # dovoli oba načina
    if token_admin != ADMIN_TOKEN and token_scheduler != SCHEDULER_TOKEN:
        abort(403)

    ok, msg = run_auto_selection()
    return msg

# --------------------------------------------------
# POŠILJANJE EMAILA – hitri trigger
# --------------------------------------------------
@app.route("/send-email-now/<int:selection_id>")
def send_email_now(selection_id):
    sel = Selection.query.get_or_404(selection_id)

    if not sel.person.email:
        flash("Ta oseba nima nastavljenega emaila — pošiljanje ni možno.", "danger")
        return redirect(url_for("index"))
    
    slot = request.args.get("slot", "manual")

    subject, body = build_email_content(sel, slot)

    send_email(sel.person.email, subject, body)

    sel.email_subject = subject
    sel.email_body = body
    sel.slot = slot
    db.session.commit()

    flash("Email je bil poslan (če je Gmail pravilno povezan).", "success")
    return redirect(url_for("index"))


# --------------------------------------------------
# EMAIL PREVIEW + CUSTOM SEND
# --------------------------------------------------
@app.route("/email-preview/<int:selection_id>")
def email_preview(selection_id):
    sel = Selection.query.get_or_404(selection_id)
    slot = request.args.get("slot", "morning")  # default

    subject, body = build_email_content(sel, slot)
    return {"subject": subject, "body": body}


@app.route("/send-email-custom/<int:selection_id>", methods=["POST"])
def send_email_custom(selection_id):
    sel = Selection.query.get_or_404(selection_id)

    # zaščita: oseba nima emaila
    if not sel.person.email:
        flash("Ta oseba nima nastavljenega emaila — pošiljanje ni možno.", "danger")
        return redirect(url_for("index"))

    slot = request.form.get("slot", "manual")
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()

    if not subject or not body:
        flash("Subject in vsebina emaila ne smeta biti prazna.", "danger")
        return redirect(url_for("index"))

    send_email(sel.person.email, subject, body)

    sel.email_subject = subject
    sel.email_body = body
    sel.slot = slot
    db.session.commit()

    flash("Prilagojen email je bil poslan.", "success")
    return redirect(url_for("index"))


# --------------------------------------------------
# PREKLOP AVTOMATIKE
# --------------------------------------------------
@app.route("/toggle_automation", methods=["POST"])
def toggle_automation():
    enabled = is_automation_enabled()
    set_setting("automation_enabled", "0" if enabled else "1")
    flash(
        "Avtomatika je bila vklopljena." if not enabled else "Avtomatika je bila izklopljena.",
        "info"
    )
    return redirect(url_for("index"))


# --------------------------------------------------
# PRISOTNOST OSEB
# --------------------------------------------------
@app.route("/toggle_presence/<int:person_id>", methods=["POST"])
def toggle_presence(person_id):
    person = Person.query.get_or_404(person_id)
    person.is_present = request.form.get("is_present") == "on"
    db.session.commit()
    return redirect(request.referrer or url_for("index"))

# --------------------------------------------------
# RESET STATISTIKE
# --------------------------------------------------
@app.route("/reset-statistics", methods=["POST"])
def reset_statistics():
    # Izbris vseh žrebov
    Selection.query.delete()
    db.session.commit()

    flash("Statistika je bila uspešno resetirana.", "info")
    return redirect(url_for("index"))

# --------------------------------------------------
# INIT DATABASE (1×)
# --------------------------------------------------
@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("Database initialized.")
