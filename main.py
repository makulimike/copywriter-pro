"""
FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM - SIMPLIFIED
Discover businesses via LinkedIn (via Apify only).
Send cold emails with phone numbers and location targeting.
No reply monitoring, no meeting scheduling.
"""

import os
import json
import csv
import sqlite3
import datetime
import time
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field, fields
from enum import Enum
import requests
from pathlib import Path
from threading import Thread, RLock
from dotenv import load_dotenv
from contextlib import contextmanager
import secrets
from urllib.parse import quote_plus, urlparse
import re
import webbrowser

load_dotenv()

# Apify
try:
    from apify_client import ApifyClient
    APIFY_AVAILABLE = True
except ImportError:
    APIFY_AVAILABLE = False
    print("⚠️ Apify client not installed. LinkedIn discovery will be disabled.")

# Flask
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_cors import CORS

# ============================================================================
# DATA MODELS
# ============================================================================

class LeadStatus(Enum):
    PENDING = "pending"
    QUALIFIED_HOT = "hot"
    QUALIFIED_MAYBE = "maybe"
    COLD = "cold"
    DEAD = "dead"

class EmailStatus(Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"

class LeadSource(Enum):
    MANUAL = "manual"
    CSV = "csv"
    APIFY = "apify_linkedin"

@dataclass
class Lead:
    lead_id: str
    campaign_id: str
    user_id: str
    name: str
    company: str
    email: str = ""
    website: str = ""
    industry: str = ""
    location: str = ""
    country: str = ""
    timezone: str = ""
    notes: str = ""
    status: str = LeadStatus.PENDING.value
    qualification_score: int = 0
    email_status: str = EmailStatus.PENDING.value
    email_sent_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    linkedin_url: str = ""
    linkedin_profile: Optional[Dict] = None
    job_title: str = ""
    phone: str = ""

@dataclass
class Campaign:
    campaign_id: str
    user_id: str
    name: str
    created_at: str
    status: str = "active"

    ideal_industries: List[str] = None
    ideal_locations: List[str] = None
    ideal_countries: List[str] = None
    ideal_company_size: str = "any"
    ideal_job_titles: List[str] = None
    search_globally: bool = True

    email_subject: str = ""
    email_body: str = ""
    notify_email: str = ""

    def __post_init__(self):
        if self.ideal_industries is None:
            self.ideal_industries = []
        if self.ideal_locations is None:
            self.ideal_locations = []
        if self.ideal_countries is None:
            self.ideal_countries = []
        if self.ideal_job_titles is None:
            self.ideal_job_titles = []

    @classmethod
    def from_dict(cls, data: dict) -> 'Campaign':
        known_fields = {
            'campaign_id', 'user_id', 'name', 'created_at', 'status',
            'ideal_industries', 'ideal_locations', 'ideal_countries',
            'ideal_company_size', 'ideal_job_titles', 'search_globally',
            'email_subject', 'email_body', 'notify_email'
        }
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered_data)

@dataclass
class EmailRecord:
    email_id: str
    lead_id: str
    campaign_id: str
    user_id: str
    subject: str
    body: str
    sent_at: str
    status: str
    error_message: Optional[str] = None

@dataclass
class User:
    user_id: str
    username: str
    password_hash: str
    email: str
    created_at: str
    campaigns: List[str] = None
    email_host: str = "smtp.gmail.com"
    email_user: str = ""
    email_password: str = ""
    apify_api_token: str = ""

    def __post_init__(self):
        if self.campaigns is None:
            self.campaigns = []

# ============================================================================
# DATABASE
# ============================================================================

class Database:
    def __init__(self, db_path="copywriter.db"):
        self.db_path = db_path
        self.lock = RLock()
        self.init_db()
        self.migrate_database()

    @contextmanager
    def get_connection(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def execute_insert(self, query: str, params: tuple) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.lastrowid

    def execute_update(self, query: str, params: tuple) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.rowcount

    def column_exists(self, table_name: str, column_name: str) -> bool:
        cols = self.execute_query(f"PRAGMA table_info({table_name})")
        return any(col['name'] == column_name for col in cols)

    def migrate_database(self):
        tables = self.execute_query("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = [t['name'] for t in tables]

        if 'users' in table_names:
            for col in ['email_host', 'email_user', 'email_password', 'apify_api_token']:
                if not self.column_exists('users', col):
                    self.execute_query(f"ALTER TABLE users ADD COLUMN {col} TEXT")

        if 'leads' in table_names:
            for col in ['linkedin_url', 'linkedin_profile', 'source', 'job_title', 'phone']:
                if not self.column_exists('leads', col):
                    self.execute_query(f"ALTER TABLE leads ADD COLUMN {col} TEXT")

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    email TEXT NOT NULL,
                    email_host TEXT DEFAULT 'smtp.gmail.com',
                    email_user TEXT,
                    email_password TEXT,
                    apify_api_token TEXT,
                    created_at TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    config TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    company TEXT,
                    email TEXT,
                    website TEXT,
                    industry TEXT,
                    location TEXT,
                    country TEXT,
                    timezone TEXT,
                    notes TEXT,
                    status TEXT NOT NULL,
                    qualification_score INTEGER DEFAULT 0,
                    email_status TEXT DEFAULT 'pending',
                    email_sent_at TEXT,
                    linkedin_url TEXT,
                    linkedin_profile TEXT,
                    source TEXT DEFAULT 'manual',
                    job_title TEXT,
                    phone TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS emails (
                    email_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    FOREIGN KEY (lead_id) REFERENCES leads (lead_id)
                )
            ''')

    def _filter_to_dataclass(self, cls, data: dict) -> dict:
        valid_keys = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in valid_keys}

    def create_user(self, user: User):
        self.execute_insert('''
            INSERT INTO users (user_id, username, password_hash, email, email_host, email_user, email_password,
                               apify_api_token, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user.user_id, user.username, user.password_hash, user.email, user.email_host,
              user.email_user or '', user.email_password or '', user.apify_api_token or '', user.created_at))

    def get_user_by_username(self, username: str) -> Optional[User]:
        row = self.execute_query('SELECT * FROM users WHERE username = ?', (username,))
        if row:
            filtered = self._filter_to_dataclass(User, row[0])
            return User(**filtered)
        return None

    def get_user(self, user_id: str) -> Optional[User]:
        row = self.execute_query('SELECT * FROM users WHERE user_id = ?', (user_id,))
        if row:
            filtered = self._filter_to_dataclass(User, row[0])
            return User(**filtered)
        return None

    def update_user_email_settings(self, user_id: str, email_host: str, email_user: str, email_password: str):
        self.execute_update('UPDATE users SET email_host = ?, email_user = ?, email_password = ? WHERE user_id = ?',
                            (email_host, email_user, email_password, user_id))

    def update_user_apify_token(self, user_id: str, api_token: str):
        self.execute_update('UPDATE users SET apify_api_token = ? WHERE user_id = ?',
                            (api_token, user_id))

    def get_user_campaigns(self, user_id: str) -> List[Campaign]:
        rows = self.execute_query('SELECT config FROM campaigns WHERE user_id = ?', (user_id,))
        campaigns = []
        for r in rows:
            try:
                data = json.loads(r['config'])
                data['user_id'] = user_id
                campaigns.append(Campaign.from_dict(data))
            except:
                continue
        return campaigns

    def save_campaign(self, user_id: str, campaign: Campaign):
        campaign.user_id = user_id
        self.execute_insert('''
            INSERT OR REPLACE INTO campaigns (campaign_id, user_id, name, config, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (campaign.campaign_id, user_id, campaign.name, json.dumps(asdict(campaign)),
              campaign.created_at, campaign.status))

    def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        row = self.execute_query('SELECT user_id, config FROM campaigns WHERE campaign_id = ?', (campaign_id,))
        if row:
            try:
                data = json.loads(row[0]['config'])
                data['user_id'] = row[0]['user_id']
                return Campaign.from_dict(data)
            except:
                return None
        return None

    def delete_campaign(self, campaign_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM emails WHERE campaign_id = ?', (campaign_id,))
            cursor.execute('DELETE FROM leads WHERE campaign_id = ?', (campaign_id,))
            cursor.execute('DELETE FROM campaigns WHERE campaign_id = ?', (campaign_id,))

    def save_leads(self, user_id: str, campaign_id: str, leads: List[Lead]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for lead in leads:
                cursor.execute('''
                    INSERT OR REPLACE INTO leads (
                        lead_id, campaign_id, user_id, name, company, email, website,
                        industry, location, country, timezone, notes, status,
                        qualification_score, email_status, email_sent_at,
                        linkedin_url, linkedin_profile, source, job_title, phone,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    lead.lead_id, campaign_id, user_id, lead.name, lead.company,
                    lead.email, lead.website, lead.industry, lead.location, lead.country, lead.timezone,
                    lead.notes, lead.status, lead.qualification_score,
                    lead.email_status, lead.email_sent_at,
                    lead.linkedin_url, json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
                    lead.source, lead.job_title, lead.phone,
                    lead.created_at, lead.updated_at
                ))

    def get_campaign_leads(self, user_id: str, campaign_id: str) -> List[Lead]:
        rows = self.execute_query('SELECT * FROM leads WHERE campaign_id = ? AND user_id = ? ORDER BY created_at DESC',
                                   (campaign_id, user_id))
        leads = []
        for r in rows:
            filtered = self._filter_to_dataclass(Lead, r)
            lead = Lead(**filtered)
            if r.get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r['linkedin_profile'])
            leads.append(lead)
        return leads

    def get_leads_with_email(self, user_id: str, campaign_id: str, limit: int = 50) -> List[Lead]:
        rows = self.execute_query('''
            SELECT * FROM leads
            WHERE campaign_id = ? AND user_id = ?
            AND email IS NOT NULL AND email != '' AND email != 'null' AND email != 'None'
            ORDER BY created_at ASC LIMIT ?
        ''', (campaign_id, user_id, limit))
        leads = []
        for r in rows:
            filtered = self._filter_to_dataclass(Lead, r)
            lead = Lead(**filtered)
            if r.get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r['linkedin_profile'])
            leads.append(lead)
        return leads

    def update_lead(self, lead: Lead):
        lead.updated_at = datetime.datetime.now().isoformat()
        self.execute_update('''
            UPDATE leads SET status = ?, qualification_score = ?,
                email_status = ?, email_sent_at = ?,
                country = ?, timezone = ?,
                linkedin_url = ?, linkedin_profile = ?, source = ?, job_title = ?, phone = ?,
                updated_at = ?
            WHERE lead_id = ?
        ''', (lead.status, lead.qualification_score,
              lead.email_status, lead.email_sent_at,
              lead.country, lead.timezone,
              lead.linkedin_url,
              json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
              lead.source, lead.job_title, lead.phone,
              lead.updated_at, lead.lead_id))

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        r = self.execute_query('SELECT * FROM leads WHERE lead_id = ?', (lead_id,))
        if r:
            filtered = self._filter_to_dataclass(Lead, r[0])
            lead = Lead(**filtered)
            if r[0].get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r[0]['linkedin_profile'])
            return lead
        return None

    def save_email(self, user_id: str, email: EmailRecord):
        email.user_id = user_id
        self.execute_insert('''
            INSERT INTO emails (email_id, lead_id, campaign_id, user_id, subject, body, sent_at, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (email.email_id, email.lead_id, email.campaign_id, user_id, email.subject, email.body,
              email.sent_at, email.status, email.error_message))

    def get_lead_emails(self, lead_id: str) -> List[EmailRecord]:
        rows = self.execute_query('SELECT * FROM emails WHERE lead_id = ? ORDER BY sent_at DESC', (lead_id,))
        emails = []
        for r in rows:
            filtered = self._filter_to_dataclass(EmailRecord, r)
            emails.append(EmailRecord(**filtered))
        return emails

# ============================================================================
# APIFY LINKEDIN DISCOVERY (Sole Data Source)
# ============================================================================

class ApifyLinkedInDiscovery:
    """LinkedIn data discovery using Apify's LinkedIn Profile Scraper"""
    
    def __init__(self, api_token=None):
        self.api_token = api_token
        self.client = ApifyClient(api_token) if api_token and APIFY_AVAILABLE else None
        self.authenticated = bool(api_token and APIFY_AVAILABLE and self.client)
    
    def set_api_token(self, api_token):
        self.api_token = api_token
        self.client = ApifyClient(api_token) if api_token and APIFY_AVAILABLE else None
        self.authenticated = bool(api_token and APIFY_AVAILABLE and self.client)
    
    def authenticate(self):
        return self.authenticated
    
    def search_people_by_company(self, company_name: str, job_titles: List[str] = None, max_results: int = 10) -> List[Dict]:
        if not self.authenticated or not self.client:
            print("❌ Apify not configured")
            return []
        
        leads = []
        try:
            print(f"🔍 Apify: Searching for employees at {company_name}")
            
            # Build search query
            search_query = company_name
            if job_titles and len(job_titles) > 0:
                search_query = f"{company_name} {job_titles[0]}"
            
            run_input = {
                "searchUrl": f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(search_query)}",
                "maxResults": max_results,
            }
            
            run = self.client.actor("drobnikj~linkedin-people-scraper").call(run_input=run_input)
            
            if run and run.get("defaultDatasetId"):
                dataset = self.client.dataset(run["defaultDatasetId"])
                for item in dataset.iterate_items():
                    # Extract name
                    name = item.get('name', '')
                    
                    # Get current company & title from experiences
                    current_company = company_name
                    job_title = ''
                    
                    experiences = item.get('experiences', [])
                    for exp in experiences:
                        if exp.get('company', '').lower() in company_name.lower() or company_name.lower() in exp.get('company', '').lower():
                            job_title = exp.get('title', '')
                            current_company = exp.get('company', company_name)
                            break
                    
                    lead = {
                        'name': name,
                        'company': current_company,
                        'job_title': job_title,
                        'linkedin_url': item.get('url', item.get('profileUrl', '')),
                        'location': item.get('location', ''),
                        'country': self._extract_country(item.get('location', '')),
                        'email': item.get('email', ''),
                        'phone': item.get('phone', ''),
                        'industry': item.get('industry', ''),
                        'source': LeadSource.APIFY.value
                    }
                    leads.append(lead)
                    
                    if len(leads) >= max_results:
                        break
                        
                print(f"✅ Apify: Found {len(leads)} employees at {company_name}")
                    
        except Exception as e:
            print(f"❌ Apify search error: {e}")
            
        return leads
    
    def _extract_country(self, location: str) -> str:
        if not location:
            return ''
        parts = location.split(',')
        return parts[-1].strip() if len(parts) > 1 else ''

# ============================================================================
# BUSINESS DISCOVERY (Apify Only)
# ============================================================================

class BusinessDiscovery:
    def __init__(self):
        pass  # No API keys needed except Apify token per user
    
    def _get_companies_for_industry(self, industry: str, limit: int = 5) -> List[str]:
        """Get sample companies for an industry (can be expanded later)"""
        industry_companies = {
            'saas': ['Salesforce', 'HubSpot', 'Zoom', 'Slack', 'Atlassian'],
            'fintech': ['Stripe', 'Square', 'PayPal', 'Robinhood', 'Revolut'],
            'e-commerce': ['Shopify', 'Amazon', 'eBay', 'Etsy', 'WooCommerce'],
            'healthtech': ['Cerner', 'Epic', 'Teladoc', 'Flatiron', 'Oscar'],
            'marketing': ['Mailchimp', 'Marketo', 'HubSpot', 'Salesforce', 'ActiveCampaign'],
            'technology': ['Microsoft', 'Google', 'Apple', 'Amazon', 'Meta'],
            'consulting': ['McKinsey', 'BCG', 'Deloitte', 'PwC', 'Accenture'],
        }
        defaults = ['TechCorp', 'InnovateInc', 'SolutionsLLC', 'GlobalEnterprises', 'NextGen']
        return industry_companies.get(industry.lower(), defaults)[:limit]
    
    def discover_businesses(self, campaign: Campaign, user_apify_token: str = None, max_businesses: int = 50) -> List[Dict]:
        if not campaign.ideal_industries:
            return []
        
        if not user_apify_token or not APIFY_AVAILABLE:
            print("❌ Apify token missing - no leads can be found")
            return []
        
        all_businesses = []
        
        try:
            apify = ApifyLinkedInDiscovery(user_apify_token)
            if not apify.authenticate():
                print("❌ Apify authentication failed")
                return []
            
            # Search each industry
            for industry in campaign.ideal_industries[:3]:
                companies = self._get_companies_for_industry(industry, limit=3)
                print(f"🔍 Searching {industry} companies: {companies}")
                
                for company in companies:
                    leads = apify.search_people_by_company(
                        company,
                        job_titles=campaign.ideal_job_titles,
                        max_results=max_businesses // 6
                    )
                    all_businesses.extend(leads)
                    time.sleep(1)  # Avoid rate limits
                    
        except Exception as e:
            print(f"❌ Discovery error: {e}")
        
        # Deduplicate
        seen = set()
        unique = []
        for b in all_businesses:
            key = f"{b.get('name')}_{b.get('company')}_{b.get('linkedin_url', '')}"
            if key not in seen:
                seen.add(key)
                unique.append(b)
        
        print(f"✅ Found {len(unique)} real leads")
        return unique[:max_businesses]

# ============================================================================
# EMAIL SERVICE
# ============================================================================

class EmailService:
    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.simulation_mode = not all([self.smtp_user, self.smtp_password])
        if self.simulation_mode:
            print("⚠️ Email simulation mode (no real emails sent)")

    def send_email(self, to_email: str, subject: str, body: str, from_name: str = "Copywriter Pro") -> bool:
        if self.simulation_mode:
            print(f"[SIMULATED] To: {to_email} | Subject: {subject}")
            return True
        try:
            msg = MIMEMultipart()
            msg['From'] = f"{from_name} <{self.smtp_user}>"
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'plain'))
            server = smtplib.SMTP(self.smtp_host, self.smtp_port)
            server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.send_message(msg)
            server.quit()
            return True
        except Exception as e:
            print(f"❌ Email failed: {e}")
            return False

    def send_campaign_email(self, lead: Lead, campaign: Campaign) -> EmailRecord:
        email_id = f"email_{int(time.time())}_{lead.lead_id}"
        timestamp = datetime.datetime.now().isoformat()
        subject = campaign.email_subject.replace("[Name]", lead.name).replace("[Company]", lead.company)
        body = campaign.email_body.replace("[Name]", lead.name).replace("[Company]", lead.company)
        if lead.job_title:
            body = body.replace("[Job Title]", lead.job_title)
        if lead.industry:
            body = body.replace("[Industry]", lead.industry)

        success = self.send_email(lead.email, subject, body)
        return EmailRecord(
            email_id=email_id,
            lead_id=lead.lead_id,
            campaign_id=campaign.campaign_id,
            user_id=lead.user_id,
            subject=subject,
            body=body,
            sent_at=timestamp,
            status=EmailStatus.SENT.value if success else EmailStatus.FAILED.value,
            error_message=None if success else "Failed to send"
        )

# ============================================================================
# LEAD PROCESSOR
# ============================================================================

class LeadProcessor:
    @staticmethod
    def import_from_csv(content: str, campaign_id: str, user_id: str) -> List[Lead]:
        leads = []
        reader = csv.DictReader(content.splitlines())
        for i, row in enumerate(reader):
            email = row.get('email', row.get('Email', '') or '').strip().lower()
            lead = Lead(
                lead_id=f"lead_{int(time.time())}_{i}_{random.randint(1000,9999)}",
                campaign_id=campaign_id,
                user_id=user_id,
                name=row.get('name', row.get('Name', 'Unknown')) or 'Unknown',
                company=row.get('company', row.get('Company', '')) or '',
                email=email,
                website=row.get('website', row.get('Website', '')) or '',
                industry=row.get('industry', row.get('Industry', '')) or '',
                location=row.get('location', row.get('Location', '')) or '',
                country=row.get('country', row.get('Country', '')) or '',
                timezone=row.get('timezone', row.get('Timezone', '')) or '',
                notes=row.get('notes', row.get('Notes', '')) or '',
                linkedin_url=row.get('linkedin', row.get('LinkedIn', '')) or '',
                job_title=row.get('job_title', row.get('Job Title', '')) or '',
                phone=row.get('phone', row.get('Phone', '')) or '',
                source=LeadSource.CSV.value,
                status=LeadStatus.PENDING.value,
                email_status=EmailStatus.PENDING.value if email and '@' in email else EmailStatus.FAILED.value,
                created_at=datetime.datetime.now().isoformat(),
                updated_at=datetime.datetime.now().isoformat()
            )
            leads.append(lead)
        return leads

    @staticmethod
    def score_lead(lead: Lead, campaign: Campaign) -> int:
        score = 0
        if campaign.ideal_industries and lead.industry and lead.industry.lower() in [i.lower() for i in campaign.ideal_industries]:
            score += 30
        if campaign.ideal_job_titles and lead.job_title and any(t.lower() in lead.job_title.lower() for t in campaign.ideal_job_titles):
            score += 20
        if campaign.ideal_locations and lead.location and lead.location.lower() in [l.lower() for l in campaign.ideal_locations]:
            score += 20
        if campaign.ideal_countries and lead.country and lead.country.lower() in [c.lower() for c in campaign.ideal_countries]:
            score += 20
        if lead.email and '@' in lead.email:
            score += 25
        if lead.website:
            score += 15
        if lead.linkedin_url:
            score += 10
        if lead.phone:
            score += 5
        return min(100, score)

# ============================================================================
# ANALYTICS
# ============================================================================

class AnalyticsEngine:
    @staticmethod
    def get_campaign_stats(db: Database, user_id: str, campaign_id: str) -> Dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            return {}
        leads = db.get_campaign_leads(user_id, campaign_id)
        
        total = len(leads)
        sent = len([l for l in leads if l.email_status == EmailStatus.SENT.value])
        hot = len([l for l in leads if l.status == LeadStatus.QUALIFIED_HOT.value])
        cold = len([l for l in leads if l.status == LeadStatus.COLD.value])
        with_phone = len([l for l in leads if l.phone])

        countries = {}
        for l in leads:
            if l.country:
                countries[l.country] = countries.get(l.country, 0) + 1
        country_str = "\n".join(f"{c}: {v}" for c, v in sorted(countries.items(), key=lambda x: x[1], reverse=True))

        return {
            'campaign_name': campaign.name,
            'total_leads': total,
            'emails_sent': sent,
            'hot_leads': hot,
            'cold_leads': cold,
            'leads_with_phone': with_phone,
            'conversion_rate': round(hot/total*100,1) if total else 0,
            'countries_found': len(countries),
            'country_breakdown': country_str
        }

# ============================================================================
# FLASK APP
# ============================================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(16))
CORS(app)

db = Database()
email_service = EmailService()
business_discovery = BusinessDiscovery()
analytics = AnalyticsEngine()

# ----------------------------------------------------------------------------
# AUTH
# ----------------------------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    user = db.get_user_by_username(username)
    if user and user.password_hash == password:
        session['user_id'] = user.user_id
        session['username'] = user.username
        flash('Login successful!', 'success')
        return redirect(url_for('dashboard'))
    flash('Invalid credentials', 'error')
    return redirect(url_for('index'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email')
        if db.get_user_by_username(username):
            flash('Username exists', 'error')
            return redirect(url_for('register'))
        user = User(
            user_id=f"user_{int(time.time())}",
            username=username,
            password_hash=password,
            email=email,
            created_at=datetime.datetime.now().isoformat()
        )
        db.create_user(user)
        flash('Registered! Please login.', 'success')
        return redirect(url_for('index'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out', 'success')
    return redirect(url_for('index'))

# ----------------------------------------------------------------------------
# APIFY SETTINGS
# ----------------------------------------------------------------------------

@app.route('/settings/apify', methods=['GET', 'POST'])
def apify_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if not user:
        session.clear()
        flash('User not found. Please log in again.', 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        apify_token = request.form.get('apify_api_token', '').strip()
        
        if apify_token:
            db.update_user_apify_token(user.user_id, apify_token)
            
            if APIFY_AVAILABLE:
                test_discovery = ApifyLinkedInDiscovery(apify_token)
                if test_discovery.authenticate():
                    flash('Apify API token saved and verified!', 'success')
                else:
                    flash('Token saved but verification failed. Please check your token.', 'warning')
            else:
                flash('Token saved. Note: Apify client not installed on server.', 'info')
        else:
            flash('Please enter a valid API token', 'error')
        
        return redirect(url_for('dashboard'))
    
    return render_template('apify_settings.html', user=user)

# ----------------------------------------------------------------------------
# EMAIL SETTINGS
# ----------------------------------------------------------------------------

@app.route('/settings/email', methods=['GET', 'POST'])
def email_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    user = db.get_user(session['user_id'])
    if not user:
        session.clear()
        flash('User not found. Please log in again.', 'error')
        return redirect(url_for('index'))
    if request.method == 'POST':
        host = request.form.get('email_host', 'smtp.gmail.com')
        user_email = request.form.get('email_user')
        password = request.form.get('email_password')
        db.update_user_email_settings(user.user_id, host, user_email, password)
        flash('Email settings saved!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('email_settings.html', user=user)

# ----------------------------------------------------------------------------
# DASHBOARD
# ----------------------------------------------------------------------------

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    user = db.get_user(session['user_id'])
    if not user:
        session.clear()
        flash('User not found. Please log in again.', 'error')
        return redirect(url_for('index'))
    campaigns = db.get_user_campaigns(session['user_id'])
    stats = [analytics.get_campaign_stats(db, session['user_id'], c.campaign_id) for c in campaigns]
    return render_template('dashboard.html', campaigns=campaigns, stats=stats, user=user)

# ----------------------------------------------------------------------------
# CAMPAIGN MANAGEMENT
# ----------------------------------------------------------------------------

@app.route('/campaign/new', methods=['GET', 'POST'])
def new_campaign():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        industries = [i.strip() for i in request.form.get('industries', '').split(',') if i.strip()]
        locations = [l.strip() for l in request.form.get('locations', '').split(',') if l.strip()]
        countries = [c.strip() for c in request.form.get('countries', '').split(',') if c.strip()]
        job_titles = [j.strip() for j in request.form.get('job_titles', '').split(',') if j.strip()]
        search_globally = request.form.get('search_globally') == 'on'

        campaign = Campaign(
            campaign_id=f"camp_{int(time.time())}",
            user_id=session['user_id'],
            name=request.form.get('name'),
            created_at=datetime.datetime.now().isoformat(),
            ideal_industries=industries,
            ideal_locations=locations,
            ideal_countries=countries,
            ideal_job_titles=job_titles,
            search_globally=search_globally,
            email_subject=request.form.get('email_subject'),
            email_body=request.form.get('email_body'),
            notify_email=request.form.get('notify_email')
        )
        db.save_campaign(session['user_id'], campaign)

        # No automatic discovery - user must click button
        flash('Campaign created! Click "Find Real Businesses" to start discovering leads.', 'success')
        return redirect(url_for('campaign_detail', campaign_id=campaign.campaign_id))

    return render_template('campaign_form.html')

@app.route('/campaign/<campaign_id>')
def campaign_detail(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
    leads = db.get_campaign_leads(session['user_id'], campaign_id)
    stats = analytics.get_campaign_stats(db, session['user_id'], campaign_id)
    return render_template('campaign_detail.html', campaign=campaign, leads=leads, stats=stats)

@app.route('/campaign/<campaign_id>/delete', methods=['POST'])
def delete_campaign(campaign_id):
    if 'user_id' in session:
        db.delete_campaign(campaign_id)
        flash('Campaign deleted', 'success')
    return redirect(url_for('dashboard'))

@app.route('/campaign/<campaign_id>/import-leads', methods=['POST'])
def import_leads(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    if 'leads_file' not in request.files:
        flash('No file', 'error')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))
    file = request.files['leads_file']
    if file.filename == '' or not file.filename.endswith('.csv'):
        flash('Please upload a CSV file', 'error')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))
    content = file.read().decode('utf-8')
    leads = LeadProcessor.import_from_csv(content, campaign_id, session['user_id'])
    campaign = db.get_campaign(campaign_id)
    for lead in leads:
        lead.qualification_score = LeadProcessor.score_lead(lead, campaign)
    db.save_leads(session['user_id'], campaign_id, leads)
    flash(f'Imported {len(leads)} leads', 'success')
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

@app.route('/campaign/<campaign_id>/send-emails', methods=['POST'])
def send_emails(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
    leads = db.get_leads_with_email(session['user_id'], campaign_id, 50)
    if not leads:
        flash('No leads with email addresses', 'info')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))

    for lead in leads:
        lead.email_status = EmailStatus.SENDING.value
        db.update_lead(lead)

    flash(f'Started sending {len(leads)} emails in background', 'success')

    uid = session['user_id']
    cid = campaign_id

    def send(uid, cid, campaign, leads):
        for lead in leads:
            email = email_service.send_campaign_email(lead, campaign)
            db.save_email(uid, email)
            lead.email_status = email.status
            lead.email_sent_at = email.sent_at
            db.update_lead(lead)
            time.sleep(1)

    Thread(target=send, args=(uid, cid, campaign, leads)).start()
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

@app.route('/campaign/<campaign_id>/discover-businesses', methods=['POST'])
def discover_businesses(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
    
    flash('Starting discovery with Apify...', 'success')

    uid = session['user_id']
    cid = campaign_id
    user = db.get_user(uid)
    apify_token = user.apify_api_token if user else None

    if not apify_token:
        flash('Please add your Apify API token in Settings first!', 'error')
        return redirect(url_for('apify_settings'))

    def discover(uid, cid, campaign, apify_token):
        discovered = business_discovery.discover_businesses(campaign, user_apify_token=apify_token, max_businesses=25)
        leads = []
        for i, biz in enumerate(discovered):
            lead = Lead(
                lead_id=f"lead_{int(time.time())}_{i}_{random.randint(1000,9999)}",
                campaign_id=cid,
                user_id=uid,
                name=biz.get('name', 'Contact'),
                company=biz.get('company', biz.get('name', 'Unknown')),
                email=biz.get('email', ''),
                website=biz.get('website', ''),
                industry=biz.get('industry', campaign.ideal_industries[0] if campaign.ideal_industries else ''),
                location=biz.get('location', ''),
                country=biz.get('country', 'United States'),
                job_title=biz.get('job_title', ''),
                linkedin_url=biz.get('linkedin_url', ''),
                linkedin_profile=biz.get('linkedin_profile'),
                source=biz.get('source', LeadSource.APIFY.value),
                status=LeadStatus.PENDING.value,
                qualification_score=50,
                phone=biz.get('phone', ''),
                created_at=datetime.datetime.now().isoformat(),
                updated_at=datetime.datetime.now().isoformat()
            )
            leads.append(lead)
        db.save_leads(uid, cid, leads)
        print(f"✅ Discovered {len(leads)} leads for {campaign.name}")

    Thread(target=discover, args=(uid, cid, campaign, apify_token)).start()
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

@app.route('/campaign/<campaign_id>/email-progress', methods=['GET'])
def email_progress(campaign_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found'}), 404
    leads = db.get_campaign_leads(session['user_id'], campaign_id)
    total_with_email = len([l for l in leads if l.email and '@' in l.email])
    sent = len([l for l in leads if l.email_status == EmailStatus.SENT.value])
    sending = len([l for l in leads if l.email_status == EmailStatus.SENDING.value])
    failed = len([l for l in leads if l.email_status == EmailStatus.FAILED.value])
    return jsonify({
        'total': total_with_email,
        'sent': sent,
        'sending': sending,
        'failed': failed,
        'campaign_name': campaign.name
    })

# ----------------------------------------------------------------------------
# LEAD ROUTES
# ----------------------------------------------------------------------------

@app.route('/lead/<lead_id>')
def lead_detail(lead_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    lead = db.get_lead(lead_id)
    if not lead:
        flash('Lead not found', 'error')
        return redirect(url_for('dashboard'))
    emails = db.get_lead_emails(lead_id)
    campaign = db.get_campaign(lead.campaign_id)
    return render_template('lead_detail.html', lead=lead, emails=emails, campaign=campaign)

@app.route('/lead/<lead_id>/send-email', methods=['POST'])
def send_lead_email(lead_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    campaign = db.get_campaign(lead.campaign_id)
    lead.email_status = EmailStatus.SENDING.value
    db.update_lead(lead)
    email = email_service.send_campaign_email(lead, campaign)
    db.save_email(session['user_id'], email)
    lead.email_status = email.status
    lead.email_sent_at = email.sent_at
    db.update_lead(lead)
    return jsonify({'status': email.status, 'message': 'Email sent' if email.status == EmailStatus.SENT.value else 'Failed'})

# ----------------------------------------------------------------------------
# ANALYTICS
# ----------------------------------------------------------------------------

@app.route('/analytics')
def analytics_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    campaigns = db.get_user_campaigns(session['user_id'])
    all_stats = [analytics.get_campaign_stats(db, session['user_id'], c.campaign_id) for c in campaigns]
    total_hot = sum(s.get('hot_leads',0) for s in all_stats)
    total_leads = sum(s.get('total_leads',0) for s in all_stats)
    total_emails = sum(s.get('emails_sent',0) for s in all_stats)
    total_with_phone = sum(s.get('leads_with_phone',0) for s in all_stats)
    return render_template('analytics.html', stats=all_stats, total_hot=total_hot,
                           total_leads=total_leads, total_emails=total_emails, 
                           total_with_phone=total_with_phone, total_campaigns=len(campaigns))

# ----------------------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------------------

@app.route('/health', methods=['GET', 'HEAD'])
def health_check():
    return '', 200

# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def create_default_user():
    if not db.get_user_by_username('admin'):
        admin = User(
            user_id=f"user_{int(time.time())}",
            username='admin',
            password_hash='admin123',
            email='admin@example.com',
            created_at=datetime.datetime.now().isoformat()
        )
        db.create_user(admin)
        print("✅ Default admin user created (admin/admin123)")

def open_browser():
    time.sleep(2)
    webbrowser.open('http://localhost:5000')

def main():
    print("="*60)
    print(" FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM")
    print("="*60)
    print("Powered by Apify LinkedIn Scraper")
    print("No API keys needed - just your Apify token")
    print("Manual discovery only - click 'Find Real Businesses'")
    create_default_user()
    print("\n✅ System ready!")
    print("🔗 http://localhost:5000")
    print("👤 Login: admin / admin123")
    print("="*60)
    
    if not os.environ.get('RENDER'):
        Thread(target=open_browser).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask app on port {port}")
    time.sleep(1)
    app.run(host='0.0.0.0', port=port, debug=True)
