"""
FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM - SIMPLIFIED
Discover businesses via LinkedIn (via Apify), Google Places, Yelp, and Clearbit.
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

# Optional dependencies
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

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
    LINKEDIN_SEARCH = "linkedin_search"
    LINKEDIN_COMPANY = "linkedin_company"
    GOOGLE_PLACES = "google_places"
    CLEARBIT = "clearbit"
    YELP = "yelp"
    APIFY = "apify_linkedin"
    SAMPLE = "sample"

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
    founded_year: Optional[int] = None
    employee_count: Optional[str] = None
    estimated_revenue: Optional[str] = None
    source: str = LeadSource.MANUAL.value
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
    min_founded_year: Optional[int] = None

    email_subject: str = ""
    email_body: str = ""

    # Notifications (optional)
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
            'ideal_company_size', 'ideal_job_titles', 'search_globally', 'min_founded_year',
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
    linkedin_username: str = ""
    linkedin_password: str = ""
    linkedin_connected: bool = False
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

    def execute_many(self, query: str, params_list: List[tuple]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(query, params_list)

    def column_exists(self, table_name: str, column_name: str) -> bool:
        cols = self.execute_query(f"PRAGMA table_info({table_name})")
        return any(col['name'] == column_name for col in cols)

    def migrate_database(self):
        tables = self.execute_query("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = [t['name'] for t in tables]

        if 'users' in table_names:
            for col in ['email_host', 'email_user', 'email_password', 'linkedin_username', 'linkedin_password', 'linkedin_connected', 'apify_api_token']:
                if not self.column_exists('users', col):
                    dtype = "INTEGER DEFAULT 0" if col == 'linkedin_connected' else "TEXT"
                    self.execute_query(f"ALTER TABLE users ADD COLUMN {col} {dtype}")

        if 'leads' in table_names:
            for col in ['linkedin_url', 'linkedin_profile', 'founded_year', 'employee_count', 'estimated_revenue', 'source', 'job_title', 'phone']:
                if not self.column_exists('leads', col):
                    dtype = "TEXT" if col != 'founded_year' else "INTEGER"
                    self.execute_query(f"ALTER TABLE leads ADD COLUMN {col} {dtype}")

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
                    linkedin_username TEXT,
                    linkedin_password TEXT,
                    linkedin_connected INTEGER DEFAULT 0,
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
                    founded_year INTEGER,
                    employee_count TEXT,
                    estimated_revenue TEXT,
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

    # ------------------------------------------------------------------------
    # Helper: filter row dict to only include dataclass fields
    # ------------------------------------------------------------------------
    def _filter_to_dataclass(self, cls, data: dict) -> dict:
        """Keep only keys that are fields of the given dataclass."""
        valid_keys = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in valid_keys}

    # ------------------------------------------------------------------------
    # User methods
    # ------------------------------------------------------------------------
    def create_user(self, user: User):
        self.execute_insert('''
            INSERT INTO users (user_id, username, password_hash, email, email_host, email_user, email_password,
                               linkedin_username, linkedin_password, linkedin_connected, apify_api_token, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user.user_id, user.username, user.password_hash, user.email, user.email_host,
              user.email_user or '', user.email_password or '',
              user.linkedin_username or '', user.linkedin_password or '',
              1 if user.linkedin_connected else 0, user.apify_api_token or '', user.created_at))

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

    def update_user_linkedin(self, user_id: str, linkedin_username: str, linkedin_password: str):
        self.execute_update('UPDATE users SET linkedin_username = ?, linkedin_password = ?, linkedin_connected = 1 WHERE user_id = ?',
                            (linkedin_username, linkedin_password, user_id))

    def update_user_apify_token(self, user_id: str, api_token: str):
        self.execute_update('UPDATE users SET apify_api_token = ? WHERE user_id = ?',
                            (api_token, user_id))

    # ------------------------------------------------------------------------
    # Campaign methods
    # ------------------------------------------------------------------------
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

    # ------------------------------------------------------------------------
    # Lead methods
    # ------------------------------------------------------------------------
    def save_leads(self, user_id: str, campaign_id: str, leads: List[Lead]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for lead in leads:
                cursor.execute('''
                    INSERT OR REPLACE INTO leads (
                        lead_id, campaign_id, user_id, name, company, email, website,
                        industry, location, country, timezone, notes, status,
                        qualification_score, email_status, email_sent_at,
                        linkedin_url, linkedin_profile, founded_year, employee_count, estimated_revenue,
                        source, job_title, phone,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    lead.lead_id, campaign_id, user_id, lead.name, lead.company,
                    lead.email, lead.website, lead.industry, lead.location, lead.country, lead.timezone,
                    lead.notes, lead.status, lead.qualification_score,
                    lead.email_status, lead.email_sent_at,
                    lead.linkedin_url, json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
                    lead.founded_year, lead.employee_count, lead.estimated_revenue,
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
                linkedin_url = ?, linkedin_profile = ?, founded_year = ?, employee_count = ?, estimated_revenue = ?,
                source = ?, job_title = ?, phone = ?,
                updated_at = ?
            WHERE lead_id = ?
        ''', (lead.status, lead.qualification_score,
              lead.email_status, lead.email_sent_at,
              lead.country, lead.timezone,
              lead.linkedin_url,
              json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
              lead.founded_year, lead.employee_count, lead.estimated_revenue,
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

    def get_leads_by_email(self, email: str) -> List[Lead]:
        rows = self.execute_query('SELECT * FROM leads WHERE email = ? ORDER BY created_at DESC', (email,))
        leads = []
        for r in rows:
            filtered = self._filter_to_dataclass(Lead, r)
            lead = Lead(**filtered)
            if r.get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r['linkedin_profile'])
            leads.append(lead)
        return leads

    # ------------------------------------------------------------------------
    # Email methods
    # ------------------------------------------------------------------------
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
# APIFY LINKEDIN DISCOVERY (Commercial LinkedIn API)
# ============================================================================

class ApifyLinkedInDiscovery:
    """LinkedIn data discovery using Apify's LinkedIn Profile Scraper"""
    
    def __init__(self, api_token=None):
        self.api_token = api_token
        self.client = ApifyClient(api_token) if api_token and APIFY_AVAILABLE else None
        self.authenticated = bool(api_token and APIFY_AVAILABLE and self.client)
    
    def set_api_token(self, api_token):
        """Update the API token for this instance"""
        self.api_token = api_token
        self.client = ApifyClient(api_token) if api_token and APIFY_AVAILABLE else None
        self.authenticated = bool(api_token and APIFY_AVAILABLE and self.client)
    
    def authenticate(self, username=None, password=None):
        """Check if API token exists and client is available"""
        return self.authenticated
    
    def search_people_by_company(self, company_name: str, job_titles: List[str] = None, max_results: int = 10) -> List[Dict]:
        """
        Search for people at a specific company using Apify's LinkedIn Company Employees scraper
        """
        if not self.authenticated or not self.client:
            print("❌ Apify not configured or client unavailable")
            return []
        
        leads = []
        try:
            print(f"🔍 Apify: Searching for employees at {company_name}")
            
            # Use Apify's LinkedIn Company Employees scraper
            # This actor gets employees from a LinkedIn company page
            run_input = {
                "company": company_name,
                "maxResults": max_results,
                "scrapeJobTitles": job_titles if job_titles else True,
                "scrapeLocations": True,
                "scrapeContactInfo": True,  # Tries to get emails/phones when available
            }
            
            # Run the actor and wait for completion
            run = self.client.actor("curious_coder~linkedin-company-employees-scraper").call(
                run_input=run_input
            )
            
            # Get results from the dataset
            if run and run.get("defaultDatasetId"):
                dataset = self.client.dataset(run["defaultDatasetId"])
                for item in dataset.iterate_items():
                    # Extract name
                    first = item.get('firstName', '')
                    last = item.get('lastName', '')
                    name = f"{first} {last}".strip()
                    if not name:
                        name = item.get('name', '')
                    
                    lead = {
                        'name': name,
                        'company': company_name,
                        'job_title': item.get('jobTitle', item.get('title', '')),
                        'linkedin_url': item.get('profileUrl', item.get('url', '')),
                        'location': item.get('location', ''),
                        'country': self._extract_country(item.get('location', '')),
                        'email': item.get('email', ''),
                        'phone': item.get('phone', item.get('phoneNumber', '')),
                        'industry': item.get('industry', ''),
                        'source': LeadSource.APIFY.value
                    }
                    leads.append(lead)
                    
                    # Limit results
                    if len(leads) >= max_results:
                        break
                        
                print(f"✅ Apify: Found {len(leads)} employees at {company_name}")
            else:
                print(f"⚠️ Apify: No results for {company_name}")
                    
        except Exception as e:
            print(f"❌ Apify search error: {e}")
            
        return leads
    
    def search_by_keywords(self, keywords: str, max_results: int = 20) -> List[Dict]:
        """
        Search LinkedIn profiles by keywords using Apify's LinkedIn Profile Scraper
        """
        if not self.authenticated or not self.client:
            return []
        
        leads = []
        try:
            print(f"🔍 Apify: Searching for '{keywords}'")
            
            # Use Apify's LinkedIn Profile Scraper with search
            run_input = {
                "searchUrl": f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(keywords)}",
                "maxResults": max_results,
            }
            
            run = self.client.actor("drobnikj~linkedin-people-scraper").call(
                run_input=run_input
            )
            
            if run and run.get("defaultDatasetId"):
                dataset = self.client.dataset(run["defaultDatasetId"])
                for item in dataset.iterate_items():
                    lead = {
                        'name': item.get('name', ''),
                        'company': item.get('company', item.get('currentCompany', '')),
                        'job_title': item.get('title', item.get('jobTitle', '')),
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
                        
                print(f"✅ Apify: Found {len(leads)} profiles for '{keywords}'")
                    
        except Exception as e:
            print(f"❌ Apify search error: {e}")
            
        return leads
    
    def _extract_country(self, location: str) -> str:
        """Extract country from location string"""
        if not location:
            return ''
        parts = location.split(',')
        return parts[-1].strip() if len(parts) > 1 else ''

# ============================================================================
# EMAIL ENRICHMENT
# ============================================================================

class EmailEnrichment:
    def __init__(self):
        self.hunter_api_key = os.getenv("HUNTER_API_KEY", "")
        self.clearbit_api_key = os.getenv("CLEARBIT_API_KEY", "")

    def find_email(self, name: str, company: str, domain: str = None) -> str:
        if self.clearbit_api_key and company:
            email = self._clearbit_email(name, company)
            if email:
                return email
        if self.hunter_api_key and (domain or company):
            email = self._hunter_email(name, domain, company)
            if email:
                return email
        if company and name:
            return self._generate_email_patterns(name, company)
        return ""

    def _clearbit_email(self, name: str, company: str) -> str:
        try:
            url = f"https://company.clearbit.com/v1/domains/find?name={quote_plus(company)}"
            headers = {'Authorization': f'Bearer {self.clearbit_api_key}'}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                domain = resp.json().get('domain', '')
                if domain:
                    first, last = name.lower().split()[0], name.lower().split()[-1]
                    for pattern in [f"{first}.{last}@{domain}", f"{first}{last}@{domain}", f"{first}@{domain}"]:
                        return pattern
        except:
            pass
        return ""

    def _hunter_email(self, name: str, domain: str, company: str) -> str:
        try:
            if not domain and company:
                url = f"https://api.hunter.io/v2/domain-search?company={quote_plus(company)}&api_key={self.hunter_api_key}"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    domain = resp.json().get('data', {}).get('domain', '')
            if domain and name:
                first, last = name.lower().split()[0], name.lower().split()[-1]
                url = f"https://api.hunter.io/v2/email-finder?domain={domain}&first_name={first}&last_name={last}&api_key={self.hunter_api_key}"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    return resp.json().get('data', {}).get('email', '')
        except:
            pass
        return ""

    def _generate_email_patterns(self, name: str, company: str) -> str:
        domain = company.lower().replace(' ', '').replace('.', '') + '.com'
        parts = name.lower().split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            return f"{first}.{last}@{domain}"
        return ""

# ============================================================================
# BUSINESS DISCOVERY (Enhanced with all sources)
# ============================================================================

class BusinessDiscovery:
    def __init__(self):
        self.google_api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
        self.clearbit_api_key = os.getenv("CLEARBIT_API_KEY", "")
        self.yelp_api_key = os.getenv("YELP_API_KEY", "")
        self.email_enricher = EmailEnrichment()
        # Apify will be created per-user with their API token

    def _get_companies_for_industry(self, industry: str, limit: int = 5) -> List[str]:
        """
        Helper method to get sample company names for an industry
        In production, you'd use Clearbit, Google, or a database
        """
        industry_companies = {
            'saas': ['Salesforce', 'HubSpot', 'Zoom', 'Slack', 'Atlassian'],
            'fintech': ['Stripe', 'Square', 'PayPal', 'Robinhood', 'Revolut'],
            'e-commerce': ['Shopify', 'Amazon', 'eBay', 'Etsy', 'WooCommerce'],
            'healthtech': ['Cerner', 'Epic', 'Teladoc', 'Flatiron', 'Oscar'],
            'marketing': ['Mailchimp', 'Marketo', 'HubSpot', 'Salesforce', 'ActiveCampaign'],
            'technology': ['Microsoft', 'Google', 'Apple', 'Amazon', 'Meta'],
            'consulting': ['McKinsey', 'BCG', 'Deloitte', 'PwC', 'Accenture'],
        }
        
        # Default fallback
        defaults = ['TechCorp', 'InnovateInc', 'SolutionsLLC', 'GlobalEnterprises', 'NextGen']
        
        companies = industry_companies.get(industry.lower(), defaults)
        return companies[:limit]

    def discover_businesses(self, campaign: Campaign, user_apify_token: str = None, max_businesses: int = 50) -> List[Dict]:
        all_businesses = []
        if not campaign.ideal_industries:
            return []

        # Apify (if user has API token)
        if user_apify_token and APIFY_AVAILABLE:
            try:
                apify = ApifyLinkedInDiscovery(user_apify_token)
                if apify.authenticate():
                    # For each industry, get some companies and search for employees
                    for industry in campaign.ideal_industries[:2]:
                        companies = self._get_companies_for_industry(industry, limit=3)
                        for company in companies:
                            leads = apify.search_people_by_company(
                                company,
                                job_titles=campaign.ideal_job_titles,
                                max_results=max_businesses // 6
                            )
                            all_businesses.extend(leads)
                            
                            # Add a small delay to avoid rate limits
                            time.sleep(1)
            except Exception as e:
                print(f"Apify error: {e}")

        # Google Places (with phone numbers)
        if self.google_api_key:
            try:
                businesses = self._search_google_places(campaign, max_businesses // 3)
                all_businesses.extend(businesses)
            except Exception as e:
                print(f"Google Places error: {e}")

        # Clearbit (with phone numbers)
        if self.clearbit_api_key:
            try:
                businesses = self._search_clearbit(campaign, max_businesses // 3)
                all_businesses.extend(businesses)
            except Exception as e:
                print(f"Clearbit error: {e}")

        # Yelp (already has phone numbers)
        if self.yelp_api_key:
            try:
                businesses = self._search_yelp(campaign, max_businesses // 3)
                all_businesses.extend(businesses)
            except Exception as e:
                print(f"Yelp error: {e}")

        # Fallback samples
        if not all_businesses:
            businesses = self._generate_samples(campaign, max_businesses)
            all_businesses.extend(businesses)

        # Deduplicate
        seen = set()
        unique = []
        for b in all_businesses:
            key = f"{b.get('name')}_{b.get('company')}_{b.get('linkedin_url', '')}"
            if key not in seen:
                seen.add(key)
                unique.append(b)

        return unique[:max_businesses]

    def _search_google_places(self, campaign: Campaign, limit: int) -> List[Dict]:
        businesses = []
        for industry in campaign.ideal_industries[:3]:
            for location in (campaign.ideal_locations or [''])[:2]:
                if not location and not campaign.search_globally:
                    continue
                search_location = location if location else "United States"
                
                url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
                params = {'query': f"{industry} companies in {search_location}", 'key': self.google_api_key}
                try:
                    resp = requests.get(url, params=params, timeout=10).json()
                    if resp.get('status') == 'OK':
                        for place in resp.get('results', [])[:limit]:
                            # Fetch details to get phone number
                            details_url = "https://maps.googleapis.com/maps/api/place/details/json"
                            details_params = {
                                'place_id': place['place_id'],
                                'fields': 'formatted_phone_number,international_phone_number',
                                'key': self.google_api_key
                            }
                            details = requests.get(details_url, params=details_params, timeout=5).json()
                            phone = details.get('result', {}).get('formatted_phone_number', '')
                            
                            # Extract country from address
                            address = place.get('formatted_address', '')
                            country = 'United States'  # Default
                            if ',' in address:
                                parts = address.split(',')
                                country = parts[-1].strip()
                            
                            businesses.append({
                                'name': f"Contact at {place.get('name', '')}",
                                'company': place.get('name', ''),
                                'location': place.get('formatted_address', '').split(',')[0],
                                'country': country,
                                'industry': industry,
                                'phone': phone,
                                'source': LeadSource.GOOGLE_PLACES.value
                            })
                except:
                    continue
        return businesses

    def _search_clearbit(self, campaign: Campaign, limit: int) -> List[Dict]:
        businesses = []
        headers = {'Authorization': f'Bearer {self.clearbit_api_key}'}
        for industry in campaign.ideal_industries[:3]:
            url = f"https://autocomplete.clearbit.com/v1/companies/suggest?query={quote_plus(industry)}"
            try:
                resp = requests.get(url, headers=headers, timeout=10).json()
                for company in resp[:limit]:
                    phone = ''
                    # Try to get more details from Company API
                    if company.get('domain'):
                        company_url = f"https://company.clearbit.com/v2/companies/find?domain={company['domain']}"
                        company_resp = requests.get(company_url, headers=headers, timeout=5)
                        if company_resp.status_code == 200:
                            company_data = company_resp.json()
                            phone = company_data.get('phone', '')
                    
                    # Determine location from company data if available
                    location = company.get('location', '')
                    country = 'United States'  # Default
                    if location and ',' in location:
                        parts = location.split(',')
                        country = parts[-1].strip()
                    
                    businesses.append({
                        'name': f"Contact at {company.get('name', '')}",
                        'company': company.get('name', ''),
                        'domain': company.get('domain', ''),
                        'website': f"https://{company.get('domain', '')}",
                        'email': f"hello@{company.get('domain', '')}",
                        'phone': phone,
                        'location': location,
                        'country': country,
                        'industry': industry,
                        'source': LeadSource.CLEARBIT.value
                    })
            except:
                continue
        return businesses

    def _search_yelp(self, campaign: Campaign, limit: int) -> List[Dict]:
        businesses = []
        headers = {'Authorization': f'Bearer {self.yelp_api_key}'}
        for industry in campaign.ideal_industries[:2]:
            for location in (campaign.ideal_locations or [''])[:2]:
                if not location and not campaign.search_globally:
                    continue
                search_location = location if location else "New York"
                
                url = "https://api.yelp.com/v3/businesses/search"
                params = {'term': industry, 'location': search_location, 'limit': limit}
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=10).json()
                    for biz in resp.get('businesses', [])[:limit]:
                        businesses.append({
                            'name': f"Contact at {biz.get('name', '')}",
                            'company': biz.get('name', ''),
                            'website': biz.get('url', ''),
                            'phone': biz.get('phone', ''),
                            'location': biz.get('location', {}).get('city', ''),
                            'country': biz.get('location', {}).get('country', ''),
                            'industry': industry,
                            'source': LeadSource.YELP.value
                        })
                except:
                    continue
        return businesses

    def _generate_samples(self, campaign: Campaign, count: int) -> List[Dict]:
        industries = campaign.ideal_industries or ['Technology', 'Marketing']
        cities = campaign.ideal_locations or ['San Francisco', 'New York', 'London']
        countries = campaign.ideal_countries or ['United States', 'United Kingdom']
        job_titles = campaign.ideal_job_titles or ['Marketing Manager', 'CEO']
        first_names = ['James', 'Mary', 'John', 'Patricia', 'Robert', 'Jennifer']
        last_names = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia']

        samples = []
        for i in range(count):
            ind = random.choice(industries)
            city = random.choice(cities) if cities else 'San Francisco'
            country = random.choice(countries) if countries else 'United States'
            first = random.choice(first_names)
            last = random.choice(last_names)
            company = f"{random.choice(['Tech', 'Smart', 'Cloud', 'Digital', 'Innovate'])}{random.choice(['Labs', 'Solutions', 'Group', 'Inc'])}"
            samples.append({
                'name': f"{first} {last}",
                'company': company,
                'job_title': random.choice(job_titles),
                'website': f"https://{company.lower()}.com",
                'email': f"{first.lower()}.{last.lower()}@{company.lower()}.com",
                'phone': f"+1 (555) {random.randint(100,999)}-{random.randint(1000,9999)}",
                'industry': ind,
                'location': city,
                'country': country,
                'source': LeadSource.SAMPLE.value
            })
        return samples

    def _extract_domain(self, website: str) -> str:
        if not website:
            return ""
        try:
            parsed = urlparse(website)
            return (parsed.netloc or parsed.path).replace('www.', '')
        except:
            return ""

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
            score += 5  # Bonus for having phone number
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
        emails = db.execute_query('SELECT * FROM emails WHERE campaign_id = ? AND user_id = ?', (campaign_id, user_id))

        total = len(leads)
        sent = len([l for l in leads if l.email_status == EmailStatus.SENT.value])
        hot = len([l for l in leads if l.status == LeadStatus.QUALIFIED_HOT.value])
        cold = len([l for l in leads if l.status == LeadStatus.COLD.value])
        with_phone = len([l for l in leads if l.phone])

        # Simple country breakdown as a string
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
email_enricher = EmailEnrichment()
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
# APIFY LINKEDIN SETTINGS
# ----------------------------------------------------------------------------

@app.route('/settings/linkedin', methods=['GET', 'POST'])
def linkedin_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if not user:
        session.clear()
        flash('User not found. Please log in again.', 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        # Save Apify API token
        apify_token = request.form.get('apify_api_token', '').strip()
        
        if apify_token:
            db.update_user_apify_token(user.user_id, apify_token)
            
            # Test the token
            if APIFY_AVAILABLE:
                test_discovery = ApifyLinkedInDiscovery(apify_token)
                if test_discovery.authenticate():
                    flash('Apify API token saved and verified! You can now search LinkedIn.', 'success')
                else:
                    flash('Token saved but verification failed. Please check your token.', 'warning')
            else:
                flash('Token saved. Note: Apify client not installed on server.', 'info')
        else:
            flash('Please enter a valid API token', 'error')
        
        return redirect(url_for('dashboard'))
    
    return render_template('linkedin_settings.html', user=user)

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

        # Capture user_id and apify token for background thread
        uid = session['user_id']
        cid = campaign.campaign_id
        user = db.get_user(uid)
        apify_token = user.apify_api_token if user else None

        def discover(uid, cid, campaign, apify_token):
            time.sleep(2)
            
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
                    source=biz.get('source', LeadSource.SAMPLE.value),
                    status=LeadStatus.PENDING.value,
                    qualification_score=50,  # We'll calculate properly when the lead is fully created
                    phone=biz.get('phone', ''),
                    created_at=datetime.datetime.now().isoformat(),
                    updated_at=datetime.datetime.now().isoformat()
                )
                leads.append(lead)
            db.save_leads(uid, cid, leads)
            print(f"✅ Discovered {len(leads)} leads for {campaign.name}")

        Thread(target=discover, args=(uid, cid, campaign, apify_token)).start()
        flash('Campaign created! Leads are being discovered in background.', 'success')
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

    # Update status to sending
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

# ----------------------------------------------------------------------------
# ADDITIONAL ROUTES TO FIX 404 ERRORS
# ----------------------------------------------------------------------------

@app.route('/campaign/<campaign_id>/discover-businesses', methods=['POST'])
def discover_businesses_alias(campaign_id):
    """Alias for discover-more to match frontend expectations."""
    return discover_more(campaign_id)

@app.route('/campaign/<campaign_id>/email-progress', methods=['GET'])
def email_progress(campaign_id):
    """Return JSON with email sending progress."""
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

@app.route('/campaign/<campaign_id>/discover-more', methods=['POST'])
def discover_more(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
    flash('Starting additional discovery...', 'success')

    uid = session['user_id']
    cid = campaign_id
    user = db.get_user(uid)
    apify_token = user.apify_api_token if user else None

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
                source=biz.get('source', LeadSource.SAMPLE.value),
                status=LeadStatus.PENDING.value,
                qualification_score=60,
                phone=biz.get('phone', ''),
                created_at=datetime.datetime.now().isoformat(),
                updated_at=datetime.datetime.now().isoformat()
            )
            leads.append(lead)
        db.save_leads(uid, cid, leads)
        print(f"✅ Added {len(leads)} more leads")

    Thread(target=discover, args=(uid, cid, campaign, apify_token)).start()
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

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

@app.route('/lead/<lead_id>/enrich', methods=['POST'])
def enrich_lead(lead_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    if not lead.email and lead.name and lead.company:
        email = email_enricher.find_email(lead.name, lead.company)
        if email:
            lead.email = email
            lead.email_status = EmailStatus.PENDING.value
            db.update_lead(lead)
            return jsonify({'success': True, 'email': email})
    return jsonify({'success': False})

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
# MAIN
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# HEALTH CHECK (for Render)
# ----------------------------------------------------------------------------

@app.route('/health', methods=['GET', 'HEAD'])
def health_check():
    """Simple health check endpoint for Render."""
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
    print(" FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM (Simplified)")
    print("="*60)
    print("Discover businesses via Google, Yelp, Clearbit, and LinkedIn (via Apify)")
    print("Phone numbers are automatically extracted when available.")
    print("Location targeting based on campaign settings.")
    print("No reply monitoring, no meeting scheduling.")
    create_default_user()
    print("\n✅ System ready!")
    print("🔗 http://localhost:5000")
    print("👤 Login: admin / admin123")
    print("="*60)
    
    # Only open browser locally, not on Render
    if not os.environ.get('RENDER'):
        Thread(target=open_browser).start()

if __name__ == "__main__":
    # Get port from environment variable (for Render) or use 5000 locally
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask app on port {port}")
    
    # Small delay to ensure everything is loaded
    time.sleep(1)
    
    # Run the app - this is the ONLY app.run() call
    app.run(host='0.0.0.0', port=port, debug=True)
