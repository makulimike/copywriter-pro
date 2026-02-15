import os
import json
import requests
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_from_directory
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from twilio.rest import Client
import facebook
import telebot  # for Telegram

load_dotenv()

app = Flask(__name__, 
            static_url_path='', 
            static_folder='.',
            template_folder='.')

# ---------------------------
# Configuration
# ---------------------------
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_USERS_TABLE = os.getenv("AIRTABLE_USERS_TABLE", "Users")
AIRTABLE_LEADS_TABLE = os.getenv("AIRTABLE_LEADS_TABLE", "Leads")
AIRTABLE_NOTIFICATIONS_TABLE = os.getenv("AIRTABLE_NOTIFICATIONS_TABLE", "Notifications")

MEMBERSTACK_API_KEY = os.getenv("MEMBERSTACK_API_KEY")
VAPI_API_KEY = os.getenv("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID")
CALENDLY_TOKEN = os.getenv("CALENDLY_TOKEN")
CALENDLY_USER_UUID = os.getenv("CALENDLY_USER_UUID")

# Twilio for SMS/WhatsApp
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# Facebook Messenger
FB_PAGE_ACCESS_TOKEN = os.getenv("FB_PAGE_ACCESS_TOKEN")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Email config
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Global flag for background thread
calling_active = True

# ---------------------------
# Helper Functions
# ---------------------------
def airtable_request(method, table, record_id=None, data=None):
    """Generic Airtable API request."""
    base_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}"
    url = f"{base_url}/{record_id}" if record_id else base_url
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.request(method, url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()

def create_airtable_record(table, fields):
    """Create a new record in Airtable."""
    data = {"fields": fields}
    return airtable_request("POST", table, data=data)

def update_airtable_record(table, record_id, fields):
    """Update an existing Airtable record."""
    data = {"fields": fields}
    return airtable_request("PATCH", table, record_id=record_id, data=data)

def get_airtable_record(table, record_id):
    """Retrieve a record by ID."""
    return airtable_request("GET", table, record_id=record_id)

def get_user_notification_settings(user_id):
    """Get user's notification preferences."""
    params = {
        "filterByFormula": f"{{UserID}} = '{user_id}'",
        "maxRecords": 1
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_NOTIFICATIONS_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    records = response.json().get("records", [])
    if records:
        return records[0]["fields"]
    return {
        "email": True,
        "sms": False,
        "whatsapp": False,
        "facebook": False,
        "telegram": False,
        "email_address": "",
        "phone_number": "",
        "whatsapp_number": "",
        "facebook_psid": "",
        "telegram_chat_id": ""
    }

def find_pending_leads(limit=5):
    """Fetch leads with status 'pending'."""
    params = {
        "filterByFormula": "{Status} = 'pending'",
        "maxRecords": limit
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_LEADS_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get("records", [])

def find_calling_leads():
    """Fetch leads that are currently in 'calling' status (stuck calls)."""
    params = {
        "filterByFormula": "{Status} = 'calling'"
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_LEADS_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get("records", [])

def create_memberstack_member(email, password):
    """Create a member in MemberStack."""
    url = "https://api.memberstack.com/v1/members"
    headers = {
        "x-api-key": MEMBERSTACK_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "email": email,
        "password": password,
        "metaData": {"plan": "premium"}
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

def send_welcome_email(to_email, name, temporary_password):
    """Send a welcome email via SMTP."""
    subject = "Welcome to The First Client Engine"
    body = f"""
    Hi {name},

    Your account is ready! Login at http://localhost:5000/dashboard

    Your temporary password is {temporary_password} (please change it).

    Next steps:
    1. Set up your notification preferences (Email, WhatsApp, SMS, Facebook)
    2. Add your first leads
    3. The AI will start calling automatically

    You'll receive instant alerts when hot leads are found!

    - The Team
    """
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

def start_vapi_call(lead_phone, lead_name, script, lead_id, user_id):
    """Start a VAPI call and track it."""
    url = "https://api.vapi.ai/call"
    headers = {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Include metadata for webhook
    payload = {
        "assistantId": VAPI_ASSISTANT_ID,
        "customer": {
            "number": lead_phone,
            "name": lead_name
        },
        "assistantOverrides": {
            "variableValues": {
                "script": script
            }
        },
        "metadata": {
            "lead_id": lead_id,
            "user_id": user_id
        }
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error starting VAPI call: {e}")
        # If call fails, reset lead status
        update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, {
            "Status": "pending",
            "UpdatedAt": datetime.utcnow().isoformat()
        })
        return None

def create_calendly_link():
    """Create a one-off Calendly scheduling link."""
    url = "https://api.calendly.com/scheduling_links"
    headers = {
        "Authorization": f"Bearer {CALENDLY_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "max_event_count": 1,
        "owner": f"https://api.calendly.com/users/{CALENDLY_USER_UUID}"
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("resource", {}).get("booking_url")
    except Exception as e:
        print(f"Error creating Calendly link: {e}")
        return None

# ---------------------------
# Notification Functions
# ---------------------------
def send_email_notification(to_email, subject, message):
    """Send email notification."""
    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(message, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def send_sms_notification(to_number, message):
    """Send SMS notification via Twilio."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_PHONE_NUMBER,
            to=to_number
        )
        return True
    except Exception as e:
        print(f"SMS error: {e}")
        return False

def send_whatsapp_notification(to_number, message):
    """Send WhatsApp notification via Twilio."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{to_number}"
        )
        return True
    except Exception as e:
        print(f"WhatsApp error: {e}")
        return False

def send_facebook_notification(psid, message):
    """Send Facebook Messenger notification."""
    try:
        url = f"https://graph.facebook.com/v12.0/me/messages?access_token={FB_PAGE_ACCESS_TOKEN}"
        payload = {
            "recipient": {"id": psid},
            "message": {"text": message}
        }
        response = requests.post(url, json=payload)
        return response.ok
    except Exception as e:
        print(f"Facebook error: {e}")
        return False

def send_telegram_notification(chat_id, message):
    """Send Telegram notification."""
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        bot.send_message(chat_id, message)
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def send_hot_lead_notification(user_id, lead_name, lead_phone, booking_link, transcript=None):
    """Send hot lead notification through all user's enabled channels."""
    settings = get_user_notification_settings(user_id)
    
    # Prepare message
    subject = "🔥 HOT LEAD DETECTED!"
    message = f"""
🔥 HOT LEAD ALERT!

Lead: {lead_name}
Phone: {lead_phone}
Booking Link: {booking_link}

Book this call now before they lose interest!
"""
    if transcript:
        message += f"\nCall Transcript:\n{transcript[:500]}..."  # First 500 chars
    
    # Send through each enabled channel
    if settings.get("email") and settings.get("email_address"):
        send_email_notification(settings["email_address"], subject, message)
    
    if settings.get("sms") and settings.get("phone_number"):
        send_sms_notification(settings["phone_number"], message[:160])  # SMS length limit
    
    if settings.get("whatsapp") and settings.get("whatsapp_number"):
        send_whatsapp_notification(settings["whatsapp_number"], message)
    
    if settings.get("facebook") and settings.get("facebook_psid"):
        send_facebook_notification(settings["facebook_psid"], message[:320])  # FB limit
    
    if settings.get("telegram") and settings.get("telegram_chat_id"):
        send_telegram_notification(settings["telegram_chat_id"], message)

def send_call_summary(user_id, lead_name, outcome, recording_url=None):
    """Send daily/weekly call summary."""
    settings = get_user_notification_settings(user_id)
    
    message = f"""
📊 Call Summary

Lead: {lead_name}
Outcome: {outcome}
"""
    if recording_url:
        message += f"Recording: {recording_url}"
    
    # Send to primary channel (email usually)
    if settings.get("email") and settings.get("email_address"):
        send_email_notification(settings["email_address"], "Call Summary", message)

# ---------------------------
# Background Calling Thread
# ---------------------------
def background_caller():
    """Background thread that continuously makes calls."""
    global calling_active
    print("Background caller started - automatically calling leads...")
    
    while calling_active:
        try:
            # Find pending leads
            pending_leads = find_pending_leads(limit=3)  # Process 3 at a time
            
            for lead in pending_leads:
                lead_id = lead["id"]
                fields = lead["fields"]
                
                lead_phone = fields.get("Phone")
                lead_name = fields.get("Name")
                script = fields.get("Script")
                user_id = fields.get("UserID")  # Assuming you have this field
                
                if not all([lead_phone, script, user_id]):
                    print(f"Lead {lead_id} missing required fields")
                    continue
                
                print(f"Starting call to {lead_name} ({lead_phone})")
                
                # Update status to calling
                update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, {
                    "Status": "calling",
                    "CalledAt": datetime.utcnow().isoformat()
                })
                
                # Start VAPI call
                vapi_response = start_vapi_call(
                    lead_phone, lead_name, script, lead_id, user_id
                )
                
                if vapi_response:
                    update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, {
                        "VapiCallId": vapi_response.get("id")
                    })
                    print(f"Call started for lead {lead_id}")
                else:
                    # Reset status if call failed
                    update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, {
                        "Status": "pending"
                    })
                
                # Wait between calls to avoid rate limits
                time.sleep(10)  # 10 seconds between calls
            
            # Check for stuck calls (in 'calling' status for too long)
            stuck_leads = find_calling_leads()
            for lead in stuck_leads:
                called_at = lead["fields"].get("CalledAt")
                if called_at:
                    called_time = datetime.fromisoformat(called_at)
                    if (datetime.utcnow() - called_time).seconds > 3600:  # 1 hour
                        # Reset stuck call
                        update_airtable_record(AIRTABLE_LEADS_TABLE, lead["id"], {
                            "Status": "pending",
                            "Notes": "Reset due to timeout"
                        })
                        print(f"Reset stuck lead {lead['id']}")
            
            # Wait before next batch
            time.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            print(f"Error in background caller: {e}")
            time.sleep(60)  # Wait longer on error

# Start background thread
caller_thread = threading.Thread(target=background_caller, daemon=True)
caller_thread.start()

# ---------------------------
# HTML Page Routes
# ---------------------------
@app.route("/")
def landing_page():
    return render_template("index.html")

@app.route("/index.html")
def index():
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")

@app.route("/campaign-setup")
def campaign_setup_page():
    return render_template("campaign-setup.html")

@app.route("/add-leads")
def add_leads_page():
    return render_template("add-leads.html")

@app.route("/notifications")
def notifications_page():
    return render_template("notifications.html")

@app.route("/results")
def results_page():
    return render_template("results.html")

# Serve static files
@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory('.', filename)

# ---------------------------
# API Routes
# ---------------------------
@app.route("/api/user/stats", methods=["GET"])
def get_user_stats():
    """Get dashboard stats"""
    return jsonify({
        "total_calls": 147,
        "hot_leads": 12,
        "booked_calls": 8,
        "conversion_rate": "5.4%"
    })

@app.route("/api/user/notifications", methods=["GET"])
def get_notification_settings():
    """Get user's notification settings"""
    user_id = request.args.get("user_id", "default_user")
    settings = get_user_notification_settings(user_id)
    return jsonify(settings)

@app.route("/api/user/notifications", methods=["POST"])
def update_notification_settings():
    """Update user's notification settings"""
    data = request.get_json()
    user_id = data.get("user_id", "default_user")
    
    # Check if settings exist
    params = {
        "filterByFormula": f"{{UserID}} = '{user_id}'"
    }
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_NOTIFICATIONS_TABLE}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(url, headers=headers, params=params)
    records = response.json().get("records", [])
    
    fields = {
        "UserID": user_id,
        "email": data.get("email", False),
        "sms": data.get("sms", False),
        "whatsapp": data.get("whatsapp", False),
        "facebook": data.get("facebook", False),
        "telegram": data.get("telegram", False),
        "email_address": data.get("email_address", ""),
        "phone_number": data.get("phone_number", ""),
        "whatsapp_number": data.get("whatsapp_number", ""),
        "facebook_psid": data.get("facebook_psid", ""),
        "telegram_chat_id": data.get("telegram_chat_id", ""),
        "UpdatedAt": datetime.utcnow().isoformat()
    }
    
    if records:
        # Update existing
        update_airtable_record(AIRTABLE_NOTIFICATIONS_TABLE, records[0]["id"], fields)
    else:
        # Create new
        create_airtable_record(AIRTABLE_NOTIFICATIONS_TABLE, fields)
    
    return jsonify({"status": "success"})

@app.route("/api/campaign/create", methods=["POST"])
def create_campaign():
    """Create a new campaign"""
    data = request.get_json()
    return jsonify({"status": "success", "campaign_id": "123"})

@app.route("/api/leads/upload", methods=["POST"])
def upload_leads():
    """Upload leads"""
    data = request.get_json()
    campaign_id = data.get("campaignId")
    leads = data.get("leads", [])
    user_id = data.get("user_id", "default_user")
    
    count = 0
    for lead in leads:
        fields = {
            "Name": lead.get("name"),
            "Phone": lead.get("phone"),
            "Status": "pending",
            "Script": data.get("script", ""),
            "UserID": user_id,
            "CampaignId": campaign_id,
            "CreatedAt": datetime.utcnow().isoformat()
        }
        create_airtable_record(AIRTABLE_LEADS_TABLE, fields)
        count += 1
    
    return jsonify({"status": "success", "count": count})

# ---------------------------
# Webhook Endpoints
# ---------------------------
@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe checkout.session.completed events."""
    payload = request.get_data(as_text=True)
    event = json.loads(payload)
    
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_email = session.get("customer_email") or session["customer_details"]["email"]
        customer_name = session["customer_details"]["name"]
        stripe_customer_id = session["customer"]

        # Create user record in Airtable
        user_fields = {
            "Email": customer_email,
            "Name": customer_name,
            "StripeCustomerId": stripe_customer_id,
            "CreatedAt": datetime.utcnow().isoformat()
        }
        
        try:
            airtable_resp = create_airtable_record(AIRTABLE_USERS_TABLE, user_fields)
            user_id = airtable_resp["id"]
            
            # Create MemberStack member
            temp_password = os.urandom(8).hex()
            memberstack_resp = create_memberstack_member(customer_email, temp_password)
            
            # Create default notification settings
            notification_fields = {
                "UserID": user_id,
                "email": True,
                "sms": False,
                "whatsapp": False,
                "facebook": False,
                "telegram": False,
                "email_address": customer_email,
                "phone_number": "",
                "whatsapp_number": "",
                "facebook_psid": "",
                "telegram_chat_id": ""
            }
            create_airtable_record(AIRTABLE_NOTIFICATIONS_TABLE, notification_fields)
            
            # Send welcome email
            send_welcome_email(customer_email, customer_name, temp_password)
            
        except Exception as e:
            print(f"Error in purchase flow: {e}")

    return jsonify({"status": "ok"}), 200

@app.route("/webhook/vapi", methods=["POST"])
def vapi_webhook():
    """Handle VAPI end-of-call reports."""
    payload = request.get_json()
    
    # Extract metadata
    metadata = payload.get("metadata", {})
    lead_id = metadata.get("lead_id")
    user_id = metadata.get("user_id")
    
    if not lead_id:
        # Try to find by call ID
        call_id = payload.get("call", {}).get("id")
        params = {
            "filterByFormula": f"{{VapiCallId}} = '{call_id}'",
            "maxRecords": 1
        }
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_LEADS_TABLE}"
        headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
        response = requests.get(url, headers=headers, params=params)
        records = response.json().get("records", [])
        if records:
            lead_id = records[0]["id"]
            user_id = records[0]["fields"].get("UserID")
    
    if not lead_id or not user_id:
        return jsonify({"error": "Could not identify lead"}), 400

    # Get lead details
    lead = get_airtable_record(AIRTABLE_LEADS_TABLE, lead_id)
    lead_fields = lead["fields"]
    lead_name = lead_fields.get("Name")
    lead_phone = lead_fields.get("Phone")

    # Extract outcome
    analysis = payload.get("analysis", {}).get("summary", {})
    outcome = analysis.get("outcome", "unknown")
    transcript = payload.get("transcript", "")
    recording_url = payload.get("recordingUrl", "")

    # Update lead
    update_fields = {
        "Status": outcome,
        "Transcript": transcript,
        "RecordingUrl": recording_url,
        "UpdatedAt": datetime.utcnow().isoformat()
    }
    update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, update_fields)

    # If hot lead, create Calendly link and send notifications
    if outcome.lower() in ["hot", "interested", "qualified"]:
        booking_link = create_calendly_link()
        if booking_link:
            # Send multi-channel notifications
            send_hot_lead_notification(
                user_id=user_id,
                lead_name=lead_name,
                lead_phone=lead_phone,
                booking_link=booking_link,
                transcript=transcript
            )

            # Update lead with booking link
            update_airtable_record(AIRTABLE_LEADS_TABLE, lead_id, {
                "Status": "booking_sent",
                "CalendlyLink": booking_link,
                "UpdatedAt": datetime.utcnow().isoformat()
            })
    else:
        # Send summary for non-hot leads (optional)
        send_call_summary(user_id, lead_name, outcome, recording_url)

    return jsonify({"status": "ok"}), 200

@app.route("/webhook/facebook", methods=["POST"])
def facebook_webhook():
    """Handle Facebook Messenger webhook for user setup."""
    data = request.get_json()
    
    if data.get("object") == "page":
        for entry in data.get("entry", []):
            for messaging in entry.get("messaging", []):
                sender_id = messaging.get("sender", {}).get("id")
                message = messaging.get("message", {}).get("text", "")
                
                if message.lower() == "connect":
                    # User wants to connect their Facebook
                    # You'd return a unique code or link to connect
                    send_facebook_notification(sender_id, 
                        "To connect your account, visit: http://localhost:5000/notifications?psid=" + sender_id)
    
    return jsonify({"status": "ok"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "calling_active": calling_active,
        "calls_made": "Background thread running"
    }), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)