import os
import random
from datetime import datetime
import json
import base64

from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, abort
)
from flask_sqlalchemy import SQLAlchemy

import requests
from email.mime.text import MIMEText   # <-- POPRAVEK TUKAJ
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# --------------------------------------------------
# Konfiguracija aplikacije
# --------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///coffee_duty.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --------------------------------------------------
# Modeli
# --------------------------------------------------
class CoffeeType(db.Model):
    __tablename__ = "coffee_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    icon = db.Column(db.String(100), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    people = db.relationship("Person", back_populates="default_coffee_type")


class Person(db.Model):
    __tablename__ = "people"

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(200), nullable=False, unique=True)
    default_coffee_type_id = db.Column(
        db.Integer, db.ForeignKey("coffee_types.id"), nullable=True
    )
    is_present = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    default_coffee_type = db.relationship("CoffeeType", back_populates="people")
    selections = db.relationship("Selection", back_populates="person")


class Selection(db.Model):
    __tablename__ = "selections"

    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("people.id"), nullable=False)
    coffee_type_id = db.Column(
        db.Integer, db.ForeignKey("coffee_types.id"),
        nullable=True
    )
    selected_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(50), default="manual")  # "manual" ali "auto"
    slot = db.Column(db.String(50), nullable=True)       # jutro/popoldne itd.
    email_subject = db.Column(db.Text, nullable=True)
    email_body = db.Column(db.Text, nullable=True)

    person = db.relationship("Person", back_populates="selections")
    coffee_type = db.relationship("CoffeeType")


class Setting(db.Model):
    """
    Preprosta key/value tabela za shranjevanje Gmail OAuth tokena
    (in po potrebi še kaj v prihodnje).
    """
    __tablename__ = "settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


# --------------------------------------------------
# Helperji za nastavitve
# --------------------------------------------------
def get_setting(key: str) -> str | None:
    s = Setting.query.get(key)
    return s.value if s else None


def set_setting(key: str, value: str) -> None:
    s = Setting.query.get(key)
    if not s:
        s = Setting(key=key, value=value)
        db.session.add(s)
    else:
        s.value = value
    db.session.commit()


# --------------------------------------------------
# Pomožne funkcije – GMAIL API POŠILJANJE
# --------------------------------------------------
def _get_gmail_credentials() -> Credentials | None:
    """Prebere credentials iz baze in vrne Google Credentials objekt."""
    token_json = get_setting("gmail_token")
    if not token_json:
        print("GMAIL API ni nastavljen – najprej obišči /authorize-gmail")
        return None

    data = json.loads(token_json)
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )
    return creds


def _save_gmail_credentials(creds: Credentials) -> None:
    """Shrani (osvežen) token nazaj v bazo."""
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    set_setting("gmail_token", json.dumps(data))


def send_email(to_email: str, subject: str, body: str) -> None:
    """
    Pošiljanje e-maila preko Gmail API (users.messages.send).
    SMTP se NE uporablja, ker ga Render blokira.
    """
    print("====== GMAIL API DEBUG START ======")
    print("TO:", to_email)
    print("SUBJECT:", subject)

    creds = _get_gmail_credentials()
    if not creds:
        print("Ni Gmail credentials – email ne bo poslan.")
        print("====== GMAIL API DEBUG END ======")
        return

    # Ustvari Gmail service
    service = build("gmail", "v1", credentials=creds)

    # Sestavi MIME sporočilo
    message = MIMEText(body, _charset="utf-8")
    message["to"] = to_email
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    body_data = {"raw": raw}

    try:
        sent = service.users().messages().send(userId="me", body=body_data).execute()
        print("Email sent, Gmail ID:", sent.get("id"))
    except Exception as e:
        print("GMAIL API EXCEPTION:", e)
    finally:
        # Če bi se token osvežil, bi bil tu že posodobljen
        _save_gmail_credentials(creds)

    print("====== GMAIL API DEBUG END ======")


def choose_random_present_person(slot: str, source: str = "auto") -> Selection | None:
    """
    Izbere naključno prisotno osebo, ustvari zapis v Selection in pošlje e-mail.
    """
    present_people = Person.query.filter_by(is_present=True, active=True).all()
    if not present_people:
        return None

    person = random.choice(present_people)
    coffee_type = person.default_coffee_type

    # Priprava subject/body v slovenščini
    time_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    slot_label = "jutranji termin" if slot == "morning" else "popoldanski termin"

    subject = f"Dežurni za kavo ({slot_label}) – {time_str}"
    body_lines = [
        "Pozdravljeni,",
        "",
        f"Za {slot_label} je dežurni za kavo:",
        f"- {person.first_name} {person.last_name}",
        "",
    ]
    if coffee_type:
        body_lines.append(
            f"Njegov/njen privzeti tip kave: {coffee_type.icon or ''} {coffee_type.name}"
        )
    body_lines.append("")
    body_lines.append("Lep pozdrav,")
    body_lines.append("Sistem za dežurno kavo ☕")

    body = "\n".join(body_lines)

    # Pošlji e-mail dežurnemu
    send_email(person.email, subject, body)

    # Shrani v bazo
    selection = Selection(
        person=person,
        coffee_type=coffee_type,
        source=source,
        slot=slot,
        email_subject=subject,
        email_body=body,
        selected_at=datetime.utcnow(),
    )
    db.session.add(selection)
    db.session.commit()
    return selection


# --------------------------------------------------
# Routes – UI
# --------------------------------------------------
@app.route("/")
def index():
    people = Person.query.filter_by(active=True).all()
    coffee_types = CoffeeType.query.filter_by(active=True).all()

    present_count = Person.query.filter_by(is_present=True, active=True).count()

    # najbolj dejaven
    best_person = (
        db.session.query(Person)
        .join(Selection)
        .group_by(Person.id)
        .order_by(db.func.count(Selection.id).desc())
        .first()
    )

    # najbolj priljubljen tip kave
    favorite_coffee = (
        db.session.query(CoffeeType)
        .join(Person, Person.default_coffee_type_id == CoffeeType.id)
        .group_by(CoffeeType.id)
        .order_by(db.func.count(Person.id).desc())
        .first()
    )

    last_selection = Selection.query.order_by(Selection.selected_at.desc()).first()

    # podatki za graf – število dežurstev na dan
    history_items = (
        db.session.query(
            db.func.date(Selection.selected_at),
            db.func.count(Selection.id),
        )
        .group_by(db.func.date(Selection.selected_at))
        .order_by(db.func.date(Selection.selected_at))
        .all()
    )

    from datetime import datetime as dt

    chart_labels = []
    for d, _ in history_items:
        if isinstance(d, str):
            d = dt.fromisoformat(d)
        chart_labels.append(d.strftime("%d.%m."))

    chart_values = [cnt for _, cnt in history_items]

    return render_template(
        "index.html",
        people=people,
        coffee_types=coffee_types,
        present_count=present_count,
        best_person=best_person,
        favorite_coffee=favorite_coffee,
        last_selection=last_selection,
        chart_labels=chart_labels,
        chart_values=chart_values,
    )


# ---------- Osebe ----------
@app.route("/people")
def people_list():
    q = request.args.get("q", "").strip()
    query = Person.query.filter_by(active=True)

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Person.first_name.ilike(like),
                Person.last_name.ilike(like),
                Person.email.ilike(like),
            )
        )

    people = query.order_by(Person.last_name, Person.first_name).all()
    coffee_types = CoffeeType.query.filter_by(active=True).all()
    return render_template(
        "people.html",
        people=people,
        coffee_types=coffee_types,
        q=q,
    )


@app.route("/people/add", methods=["POST"])
def people_add():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    default_coffee_type_id = request.form.get("default_coffee_type_id") or None
    is_present = bool(request.form.get("is_present"))

    if not first_name or not last_name or not email:
        flash("Ime, priimek in e-mail so obvezni.", "danger")
        return redirect(url_for("people_list"))

    person = Person(
        first_name=first_name,
        last_name=last_name,
        email=email,
        default_coffee_type_id=default_coffee_type_id,
        is_present=is_present,
    )
    db.session.add(person)
    db.session.commit()
    flash("Oseba je dodana.", "success")
    return redirect(url_for("people_list"))


@app.route("/people/<int:person_id>/edit", methods=["POST"])
def people_edit(person_id):
    person = Person.query.get_or_404(person_id)
    person.first_name = request.form.get("first_name", "").strip()
    person.last_name = request.form.get("last_name", "").strip()
    person.email = request.form.get("email", "").strip()
    default_coffee_type_id = request.form.get("default_coffee_type_id") or None
    person.default_coffee_type_id = default_coffee_type_id
    person.is_present = bool(request.form.get("is_present"))

    db.session.commit()
    flash("Oseba je posodobljena.", "success")
    return redirect(url_for("people_list"))


@app.route("/people/<int:person_id>/delete", methods=["POST"])
def people_delete(person_id):
    person = Person.query.get_or_404(person_id)
    person.active = False
    db.session.commit()
    flash("Oseba je odstranjena.", "success")
    return redirect(url_for("people_list"))


# ---------- Tipi kave ----------
@app.route("/coffee-types")
def coffee_types_list():
    coffee_types = CoffeeType.query.filter_by(active=True).order_by(CoffeeType.name).all()
    return render_template("coffee_types.html", coffee_types=coffee_types)


@app.route("/coffee-types/add", methods=["POST"])
def coffee_types_add():
    name = request.form.get("name", "").strip()
    icon = request.form.get("icon", "").strip()

    if not name:
        flash("Ime tipa kave je obvezno.", "danger")
        return redirect(url_for("coffee_types_list"))

    ct = CoffeeType(name=name, icon=icon)
    db.session.add(ct)
    db.session.commit()
    flash("Tip kave je dodan.", "success")
    return redirect(url_for("coffee_types_list"))


@app.route("/coffee-types/<int:ct_id>/edit", methods=["POST"])
def coffee_types_edit(ct_id):
    ct = CoffeeType.query.get_or_404(ct_id)
    ct.name = request.form.get("name", "").strip()
    ct.icon = request.form.get("icon", "").strip()
    db.session.commit()
    flash("Tip kave je posodobljen.", "success")
    return redirect(url_for("coffee_types_list"))


@app.route("/coffee-types/<int:ct_id>/delete", methods=["POST"])
def coffee_types_delete(ct_id):
    ct = CoffeeType.query.get_or_404(ct_id)
    ct.active = False
    db.session.commit()
    flash("Tip kave je odstranjen.", "success")
    return redirect(url_for("coffee_types_list"))


# ---------- Zgodovina ----------
@app.route("/history")
def history_list():
    history = (
        Selection.query
        .order_by(Selection.selected_at.desc())
        .limit(200)
        .all()
    )
    return render_template("history.html", history=history)


# ---------- Checkbox Prisotnost ----------
@app.route("/toggle-presence/<int:person_id>", methods=["POST"])
def toggle_presence(person_id):
    person = Person.query.get_or_404(person_id)
    person.is_present = bool(request.form.get("is_present"))
    db.session.commit()
    return redirect(url_for("index"))


# ---------- Ročni randomizer ----------
@app.route("/random-now", methods=["POST"])
def random_now():
    selection = choose_random_present_person(slot="manual", source="manual")
    if selection is None:
        flash("Ni nobene prisotne osebe.", "warning")
    else:
        flash(
            f"Izbran je bil: {selection.person.first_name} "
            f"{selection.person.last_name}.",
            "success",
        )
    return redirect(url_for("index"))


# ---------- TEST EMAIL ----------
@app.route("/test-email")
def test_email():
    try:
        send_email(
            "aljaz.lederer@tps-imp.si",
            "GMAIL API TEST – Coffee Duty",
            "To je testni email iz Coffee Duty sistema (Gmail API).",
        )
        return "OK – Email POSLAN (če je Gmail API nastavljen prav)"
    except Exception as e:
        return f"NAPAKA: {e}"


# ---------- Scheduler endpoint ----------
@app.route("/run-scheduler")
def run_scheduler():
    # Preprosta zaščita s tokenom v URL-ju
    token = request.args.get("token")
    expected = os.environ.get("SCHEDULER_TOKEN")
    if not expected or token != expected:
        abort(403)

    slot = request.args.get("slot", "morning")  # "morning" ali "afternoon"

    # Preveri, če je delovni dan (pon–pet)
    today_weekday = datetime.now().weekday()  # 0=Mon, 6=Sun
    if today_weekday > 4:
        return "Ni delovni dan – nič ne naredim.", 200

    selection = choose_random_present_person(slot=slot, source="auto")
    if selection is None:
        return "Ni prisotnih oseb.", 200

    return f"Izbran: {selection.person.first_name} {selection.person.last_name}", 200


# ---------- API: Random za frontend ----------
@app.route("/random")
def random_api():
    """
    Frontend randomizer – vrne JSON.
    Ne pošilja emailov, ne redirecta, samo UI update.
    """
    present_people = Person.query.filter_by(is_present=True, active=True).all()
    if not present_people:
        return {"error": "Ni prisotnih oseb."}

    person = random.choice(present_people)
    coffee_type = person.default_coffee_type

    # zapiši v bazo
    selection = Selection(
        person=person,
        coffee_type=coffee_type,
        source="manual",       # frontend klik
        slot="manual",
        selected_at=datetime.utcnow(),
    )
    db.session.add(selection)
    db.session.commit()

    return {
        "person_id": person.id,
        "person_name": f"{person.first_name} {person.last_name}",
    }


# ---------- Debug env ----------
@app.route("/debug-env")
def debug_env():
    return {
        "scheduler_token_env": os.environ.get("SCHEDULER_TOKEN"),
        "received_token": request.args.get("token"),
    }

# ---------- Gmail OAuth – začetek ----------
@app.route("/authorize-gmail")
def authorize_gmail():
    """
    Ročno sprožiš Gmail OAuth flow.
    Pokličeš: https://coffee-duty.onrender.com/authorize-gmail
    """
    from urllib.parse import urlencode, quote_plus

    client_id = os.environ["GMAIL_CLIENT_ID"]
    redirect_uri = os.environ.get(
        "GMAIL_REDIRECT_URI",
        "https://coffee-duty.onrender.com/oauth2callback",
    )

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.send",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }

    # 🔥 redirect_uri mora biti obvezno URL-encoded
    params["redirect_uri"] = quote_plus(redirect_uri)

    # 🔥 scope mora biti prav tako encoded
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@app.route("/oauth2callback")
def oauth2callback():
    """
    Google pokliče ta endpoint po uspešnem loginu.
    Tu zamenjamo 'code' za token in ga shranimo v bazo.
    """
    from urllib.parse import quote_plus

    error = request.args.get("error")
    if error:
        return f"Error from Google OAuth: {error}", 400

    code = request.args.get("code")
    if not code:
        return "Error: Missing code", 400

    client_id = os.environ["GMAIL_CLIENT_ID"]
    client_secret = os.environ["GMAIL_CLIENT_SECRET"]

    redirect_uri_raw = os.environ.get(
        "GMAIL_REDIRECT_URI",
        "https://coffee-duty.onrender.com/oauth2callback",
    )

    # 🔥 Za token exchange redirect_uri ne sme biti encoded
    redirect_uri = redirect_uri_raw

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    r = requests.post(token_url, data=data)
    token_response = r.json()

    if "error" in token_response:
        return f"Token error: {token_response}", 400

    # Sestavi Credentials in shrani v DB
    creds = Credentials(
        token=token_response["access_token"],
        refresh_token=token_response.get("refresh_token"),
        token_uri=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    _save_gmail_credentials(creds)

    return (
        "Gmail pošiljanje je uspešno nastavljeno. "
        "Lahko zapreš to okno in se vrneš v Coffee Duty."
    )


# --------------------------------------------------
# Inicializacija baze
# --------------------------------------------------
@app.cli.command("init-db")
def init_db():
    """Flask CLI: flask init-db"""
    db.create_all()
    print("Baza inicializirana.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
