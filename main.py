"""
FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM - MULTI-CHANNEL VERSION
Discover businesses via Google Places API and reach out via:
- Email
- WhatsApp
- Facebook Messenger
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
import webbrowser
import re
import urllib.parse

load_dotenv()

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

class ChannelType(Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    FACEBOOK = "facebook"
    SMS = "sms"
    LINKEDIN = "linkedin"

class MessageStatus(Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    READ = "read"
    REPLIED = "replied"

class LeadSource(Enum):
    MANUAL = "manual"
    CSV = "csv"
    GOOGLE_PLACES = "google_places"
    MANUAL_SEARCH = "manual_search"
    FACEBOOK = "facebook"
    WHATSAPP = "whatsapp"

@dataclass
class Lead:
    lead_id: str
    campaign_id: str
    user_id: str
    name: str
    company: str
    email: str = ""
    phone: str = ""  # For WhatsApp/SMS
    facebook_url: str = ""  # For Facebook Messenger
    facebook_id: str = ""  # Facebook Page/Profile ID
    website: str = ""
    industry: str = ""
    location: str = ""
    country: str = ""
    timezone: str = ""
    notes: str = ""
    status: str = LeadStatus.PENDING.value
    qualification_score: int = 0
    created_at: str = ""
    updated_at: str = ""
    linkedin_url: str = ""
    linkedin_profile: Optional[Dict] = None
    job_title: str = ""
    source: str = LeadSource.MANUAL.value
    place_id: str = ""  # Google Place ID
    rating: float = 0.0
    total_ratings: int = 0
    price_level: int = 0
    business_status: str = ""
    types: str = ""  # Business types/categories
    preferred_channel: str = ChannelType.EMAIL.value  # Default channel
    last_contacted: Optional[str] = None
    contact_attempts: int = 0

@dataclass
class Campaign:
    campaign_id: str
    user_id: str
    name: str
    created_at: str
    status: str = "active"

    # Search parameters
    search_queries: List[str] = None
    search_locations: List[str] = None
    max_results_per_search: int = 20
    
    # Qualification criteria
    ideal_industries: List[str] = None
    min_rating: float = 0.0
    max_results: int = 100

    # Multi-channel settings
    channels_enabled: List[str] = None  # ['email', 'whatsapp', 'facebook']
    
    # Email settings
    email_subject: str = ""
    email_body: str = ""
    
    # WhatsApp settings
    whatsapp_template: str = ""
    whatsapp_enabled: bool = False
    
    # Facebook settings
    facebook_template: str = ""
    facebook_enabled: bool = False
    
    # Notification settings
    notify_email: str = ""

    def __post_init__(self):
        if self.search_queries is None:
            self.search_queries = []
        if self.search_locations is None:
            self.search_locations = []
        if self.ideal_industries is None:
            self.ideal_industries = []
        if self.channels_enabled is None:
            self.channels_enabled = ['email']

    @classmethod
    def from_dict(cls, data: dict) -> 'Campaign':
        known_fields = {
            'campaign_id', 'user_id', 'name', 'created_at', 'status',
            'search_queries', 'search_locations', 'max_results_per_search',
            'ideal_industries', 'min_rating', 'max_results',
            'channels_enabled', 'email_subject', 'email_body',
            'whatsapp_template', 'whatsapp_enabled',
            'facebook_template', 'facebook_enabled',
            'notify_email'
        }
        filtered_data = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered_data)

@dataclass
class MessageRecord:
    message_id: str
    lead_id: str
    campaign_id: str
    user_id: str
    channel: str  # email, whatsapp, facebook
    content: str
    sent_at: str
    status: str
    error_message: Optional[str] = None
    read_at: Optional[str] = None
    replied_at: Optional[str] = None

@dataclass
class User:
    user_id: str
    username: str
    password_hash: str
    email: str
    created_at: str
    campaigns: List[str] = None
    
    # Email settings
    email_host: str = "smtp.gmail.com"
    email_user: str = ""
    email_password: str = ""
    
    # Google Places API
    google_places_api_key: str = ""
    
    # WhatsApp Business API (or Twilio for SMS/WhatsApp)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""  # Format: whatsapp:+14155238886
    
    # Facebook Messenger settings
    facebook_page_id: str = ""
    facebook_page_token: str = ""
    
    # Default sender name
    sender_name: str = ""

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

        # Users table migrations
        if 'users' in table_names:
            new_user_cols = ['twilio_account_sid', 'twilio_auth_token', 'twilio_whatsapp_number',
                            'facebook_page_id', 'facebook_page_token', 'sender_name']
            for col in new_user_cols:
                if not self.column_exists('users', col):
                    try:
                        self.execute_query(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                    except:
                        pass

        # Leads table migrations
        if 'leads' in table_names:
            new_lead_cols = ['phone', 'facebook_url', 'facebook_id', 'preferred_channel', 
                            'last_contacted', 'contact_attempts']
            for col in new_lead_cols:
                if not self.column_exists('leads', col):
                    try:
                        self.execute_query(f"ALTER TABLE leads ADD COLUMN {col} TEXT")
                    except:
                        pass

        # Handle messages table
        if 'messages' not in table_names:
            # Create messages table if it doesn't exist
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS messages (
                        message_id TEXT PRIMARY KEY,
                        lead_id TEXT NOT NULL,
                        campaign_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        content TEXT NOT NULL,
                        sent_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        read_at TEXT,
                        replied_at TEXT,
                        FOREIGN KEY (lead_id) REFERENCES leads (lead_id)
                    )
                ''')
        else:
            # Check if messages table needs new columns
            msg_columns = [col['name'] for col in self.execute_query("PRAGMA table_info(messages)")]
            for col in ['read_at', 'replied_at']:
                if col not in msg_columns:
                    try:
                        self.execute_query(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
                    except:
                        pass

    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    email TEXT NOT NULL,
                    email_host TEXT DEFAULT 'smtp.gmail.com',
                    email_user TEXT,
                    email_password TEXT,
                    google_places_api_key TEXT,
                    twilio_account_sid TEXT,
                    twilio_auth_token TEXT,
                    twilio_whatsapp_number TEXT,
                    facebook_page_id TEXT,
                    facebook_page_token TEXT,
                    sender_name TEXT,
                    created_at TEXT NOT NULL
                )
            ''')
            
            # Campaigns table
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
            
            # Leads table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    company TEXT,
                    email TEXT,
                    phone TEXT,
                    facebook_url TEXT,
                    facebook_id TEXT,
                    website TEXT,
                    industry TEXT,
                    location TEXT,
                    country TEXT,
                    timezone TEXT,
                    notes TEXT,
                    status TEXT NOT NULL,
                    qualification_score INTEGER DEFAULT 0,
                    preferred_channel TEXT DEFAULT 'email',
                    last_contacted TEXT,
                    contact_attempts INTEGER DEFAULT 0,
                    linkedin_url TEXT,
                    linkedin_profile TEXT,
                    source TEXT DEFAULT 'manual',
                    job_title TEXT,
                    place_id TEXT,
                    rating REAL DEFAULT 0,
                    total_ratings INTEGER DEFAULT 0,
                    price_level INTEGER DEFAULT 0,
                    business_status TEXT,
                    types TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Messages table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    lead_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    read_at TEXT,
                    replied_at TEXT,
                    FOREIGN KEY (lead_id) REFERENCES leads (lead_id)
                )
            ''')

    def _filter_to_dataclass(self, cls, data: dict) -> dict:
        valid_keys = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in valid_keys}

    def create_user(self, user: User):
        self.execute_insert('''
            INSERT INTO users (
                user_id, username, password_hash, email, 
                email_host, email_user, email_password,
                google_places_api_key,
                twilio_account_sid, twilio_auth_token, twilio_whatsapp_number,
                facebook_page_id, facebook_page_token,
                sender_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user.user_id, user.username, user.password_hash, user.email,
            user.email_host, user.email_user or '', user.email_password or '',
            user.google_places_api_key or '',
            user.twilio_account_sid or '', user.twilio_auth_token or '', user.twilio_whatsapp_number or '',
            user.facebook_page_id or '', user.facebook_page_token or '',
            user.sender_name or '', user.created_at
        ))

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

    def update_user(self, user_id: str, **kwargs):
        sets = []
        values = []
        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            values.append(value)
        values.append(user_id)
        query = f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?"
        self.execute_update(query, tuple(values))

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
            cursor.execute('DELETE FROM messages WHERE campaign_id = ?', (campaign_id,))
            cursor.execute('DELETE FROM leads WHERE campaign_id = ?', (campaign_id,))
            cursor.execute('DELETE FROM campaigns WHERE campaign_id = ?', (campaign_id,))

    def save_leads(self, user_id: str, campaign_id: str, leads: List[Lead]):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for lead in leads:
                # Ensure rating is not None
                rating = lead.rating if lead.rating is not None else 0.0
                total_ratings = lead.total_ratings if lead.total_ratings is not None else 0
                price_level = lead.price_level if lead.price_level is not None else 0
                
                cursor.execute('''
                    INSERT OR REPLACE INTO leads (
                        lead_id, campaign_id, user_id, name, company, email, phone,
                        facebook_url, facebook_id, website, industry, location, country,
                        timezone, notes, status, qualification_score, preferred_channel,
                        last_contacted, contact_attempts, linkedin_url, linkedin_profile,
                        source, job_title, place_id, rating, total_ratings, price_level,
                        business_status, types, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    lead.lead_id, campaign_id, user_id, lead.name, lead.company,
                    lead.email, lead.phone, lead.facebook_url, lead.facebook_id,
                    lead.website, lead.industry, lead.location, lead.country,
                    lead.timezone, lead.notes, lead.status, lead.qualification_score,
                    lead.preferred_channel, lead.last_contacted, lead.contact_attempts,
                    lead.linkedin_url, json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
                    lead.source, lead.job_title, lead.place_id, rating,
                    total_ratings, price_level, lead.business_status,
                    lead.types, lead.created_at, lead.updated_at
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
            # Ensure rating is float
            if lead.rating is None:
                lead.rating = 0.0
            leads.append(lead)
        return leads

    def get_leads_by_channel(self, user_id: str, campaign_id: str, channel: str, limit: int = 50) -> List[Lead]:
        """Get leads that have contact info for a specific channel"""
        if channel == ChannelType.EMAIL.value:
            condition = "email IS NOT NULL AND email != '' AND email != 'null' AND email != 'None'"
        elif channel == ChannelType.WHATSAPP.value:
            condition = "phone IS NOT NULL AND phone != '' AND phone != 'null' AND phone != 'None'"
        elif channel == ChannelType.FACEBOOK.value:
            condition = "(facebook_url IS NOT NULL AND facebook_url != '') OR (facebook_id IS NOT NULL AND facebook_id != '')"
        else:
            return []
        
        rows = self.execute_query(f'''
            SELECT * FROM leads
            WHERE campaign_id = ? AND user_id = ? AND {condition}
            ORDER BY created_at ASC LIMIT ?
        ''', (campaign_id, user_id, limit))
        
        leads = []
        for r in rows:
            filtered = self._filter_to_dataclass(Lead, r)
            lead = Lead(**filtered)
            if r.get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r['linkedin_profile'])
            if lead.rating is None:
                lead.rating = 0.0
            leads.append(lead)
        return leads

    def update_lead(self, lead: Lead):
        lead.updated_at = datetime.datetime.now().isoformat()
        rating = lead.rating if lead.rating is not None else 0.0
        total_ratings = lead.total_ratings if lead.total_ratings is not None else 0
        price_level = lead.price_level if lead.price_level is not None else 0
        
        self.execute_update('''
            UPDATE leads SET 
                status = ?, qualification_score = ?, preferred_channel = ?,
                last_contacted = ?, contact_attempts = ?,
                country = ?, timezone = ?, linkedin_url = ?, linkedin_profile = ?,
                source = ?, job_title = ?, phone = ?, facebook_url = ?, facebook_id = ?,
                rating = ?, total_ratings = ?, price_level = ?, business_status = ?, types = ?,
                updated_at = ?
            WHERE lead_id = ?
        ''', (
            lead.status, lead.qualification_score, lead.preferred_channel,
            lead.last_contacted, lead.contact_attempts,
            lead.country, lead.timezone, lead.linkedin_url,
            json.dumps(lead.linkedin_profile) if lead.linkedin_profile else None,
            lead.source, lead.job_title, lead.phone, lead.facebook_url, lead.facebook_id,
            rating, total_ratings, price_level, lead.business_status, lead.types,
            lead.updated_at, lead.lead_id
        ))

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        r = self.execute_query('SELECT * FROM leads WHERE lead_id = ?', (lead_id,))
        if r:
            filtered = self._filter_to_dataclass(Lead, r[0])
            lead = Lead(**filtered)
            if r[0].get('linkedin_profile'):
                lead.linkedin_profile = json.loads(r[0]['linkedin_profile'])
            if lead.rating is None:
                lead.rating = 0.0
            return lead
        return None

    def save_message(self, user_id: str, message: MessageRecord):
        message.user_id = user_id
        self.execute_insert('''
            INSERT INTO messages (
                message_id, lead_id, campaign_id, user_id, channel,
                content, sent_at, status, error_message, read_at, replied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            message.message_id, message.lead_id, message.campaign_id, user_id,
            message.channel, message.content, message.sent_at, message.status,
            message.error_message, message.read_at, message.replied_at
        ))

    def get_lead_messages(self, lead_id: str) -> List[MessageRecord]:
        rows = self.execute_query('SELECT * FROM messages WHERE lead_id = ? ORDER BY sent_at DESC', (lead_id,))
        messages = []
        for r in rows:
            filtered = self._filter_to_dataclass(MessageRecord, r)
            messages.append(MessageRecord(**filtered))
        return messages

    def update_message_status(self, message_id: str, status: str, **kwargs):
        sets = ["status = ?"]
        values = [status]
        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            values.append(value)
        values.append(message_id)
        query = f"UPDATE messages SET {', '.join(sets)} WHERE message_id = ?"
        self.execute_update(query, tuple(values))

# ============================================================================
# GOOGLE PLACES API DISCOVERY
# ============================================================================

class GooglePlacesDiscovery:
    """Lead discovery using Google Places API"""
    
    BASE_URL = "https://maps.googleapis.com/maps/api/place"
    
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.authenticated = bool(api_key)
    
    def set_api_key(self, api_key):
        self.api_key = api_key
        self.authenticated = bool(api_key)
    
    def search_places(self, query: str, location: str = "", max_results: int = 20) -> List[Dict]:
        """Search for businesses using Google Places API"""
        if not self.authenticated or not self.api_key:
            print("❌ Google Places API key not configured")
            return []
        
        businesses = []
        
        try:
            print(f"🔍 Google Places: Searching for '{query}' in '{location or 'any'}'")
            
            # Text Search to find places
            search_url = f"{self.BASE_URL}/textsearch/json"
            search_text = f"{query} in {location}" if location else query
            
            params = {
                'query': search_text,
                'key': self.api_key,
                'maxresults': min(max_results, 20)
            }
            
            response = requests.get(search_url, params=params)
            data = response.json()
            
            if data.get('status') != 'OK' and data.get('status') != 'ZERO_RESULTS':
                print(f"⚠️ Google Places API error: {data.get('status')}")
                return []
            
            places = data.get('results', [])
            print(f"✅ Found {len(places)} places in initial search")
            
            # Get details for each place
            for place in places[:max_results]:
                place_id = place.get('place_id')
                if not place_id:
                    continue
                
                # Get place details
                details_url = f"{self.BASE_URL}/details/json"
                details_params = {
                    'place_id': place_id,
                    'fields': 'name,formatted_address,formatted_phone_number,website,rating,user_ratings_total,price_level,business_status,types,url',
                    'key': self.api_key
                }
                
                details_response = requests.get(details_url, params=details_params)
                details_data = details_response.json()
                
                if details_data.get('status') == 'OK':
                    result = details_data.get('result', {})
                    
                    # Extract location components
                    address = result.get('formatted_address', '')
                    country = self._extract_country(address)
                    
                    # Get business types
                    types = result.get('types', [])
                    primary_type = self._get_primary_business_type(types)
                    
                    # Try to find Facebook URL from website (if any)
                    facebook_url = self._find_facebook_url(result.get('website', ''))
                    
                    # Ensure rating is not None
                    rating = result.get('rating', 0)
                    if rating is None:
                        rating = 0
                    
                    business = {
                        'name': result.get('name', place.get('name', '')),
                        'company': result.get('name', place.get('name', '')),
                        'address': address,
                        'location': address,
                        'country': country,
                        'phone': self._format_phone_for_whatsapp(result.get('formatted_phone_number', '')),
                        'website': result.get('website', ''),
                        'email': '',  # Email not available from Places API
                        'facebook_url': facebook_url,
                        'industry': primary_type,
                        'place_id': place_id,
                        'rating': rating,
                        'total_ratings': result.get('user_ratings_total', 0),
                        'price_level': result.get('price_level', 0),
                        'business_status': result.get('business_status', ''),
                        'types': ','.join(types[:5]),
                        'google_maps_url': result.get('url', ''),
                        'source': LeadSource.GOOGLE_PLACES.value
                    }
                    
                    businesses.append(business)
                    time.sleep(0.1)
            
            print(f"✅ Google Places: Found {len(businesses)} businesses with details")
            
        except Exception as e:
            print(f"❌ Google Places API error: {e}")
            import traceback
            traceback.print_exc()
        
        return businesses
    
    def _format_phone_for_whatsapp(self, phone: str) -> str:
        """Format phone number for WhatsApp (remove non-digits)"""
        if not phone:
            return ''
        # Extract digits only
        digits = re.sub(r'\D', '', phone)
        # Add country code if missing (assuming US/CA for now)
        if len(digits) == 10:
            digits = '1' + digits
        return digits
    
    def _find_facebook_url(self, website: str) -> str:
        """Try to find Facebook URL from website"""
        if not website:
            return ''
        
        # If the website itself is Facebook
        if 'facebook.com' in website.lower():
            return website
        
        # In a real implementation, you might scrape the website or use other APIs
        # For now, return empty
        return ''
    
    def _extract_country(self, address: str) -> str:
        """Extract country from address string"""
        if not address:
            return ''
        parts = address.split(',')
        return parts[-1].strip() if len(parts) > 1 else ''
    
    def _get_primary_business_type(self, types: List[str]) -> str:
        """Get the primary business type from types list"""
        exclude = ['establishment', 'point_of_interest', 'food', 'store']
        for t in types:
            if t not in exclude and '_' not in t:
                return t.replace('_', ' ').title()
        return types[0].replace('_', ' ').title() if types else 'Business'

# ============================================================================
# MESSAGE SERVICE (Multi-channel)
# ============================================================================

class MessageService:
    def __init__(self):
        # Email settings
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        
        # Simulation mode
        self.simulation_mode = not all([self.smtp_user, self.smtp_password])
        if self.simulation_mode:
            print("⚠️ Message simulation mode (no real messages sent)")
    
    def send_email(self, to_email: str, subject: str, body: str, from_name: str = "Copywriter Pro") -> bool:
        """Send email via SMTP"""
        if self.simulation_mode:
            print(f"[SIMULATED EMAIL] To: {to_email} | Subject: {subject}")
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
    
    def send_whatsapp(self, to_phone: str, message: str, twilio_settings: Dict = None) -> bool:
        """
        Send WhatsApp message via Twilio
        
        Args:
            to_phone: Phone number with country code (e.g., 1234567890)
            message: Message content
            twilio_settings: Dict with account_sid, auth_token, from_number
        """
        if self.simulation_mode or not twilio_settings:
            print(f"[SIMULATED WHATSAPP] To: {to_phone} | Message: {message[:50]}...")
            return True
        
        try:
            # This requires the twilio package: pip install twilio
            from twilio.rest import Client
            
            account_sid = twilio_settings.get('account_sid')
            auth_token = twilio_settings.get('auth_token')
            from_number = twilio_settings.get('from_number')  # Format: whatsapp:+14155238886
            
            if not all([account_sid, auth_token, from_number]):
                print("❌ Twilio settings incomplete")
                return False
            
            client = Client(account_sid, auth_token)
            
            # Format to number for WhatsApp
            to_whatsapp = f"whatsapp:+{to_phone}"
            
            message_obj = client.messages.create(
                body=message,
                from_=from_number,
                to=to_whatsapp
            )
            
            return message_obj.sid is not None
            
        except ImportError:
            print("❌ Twilio package not installed. Run: pip install twilio")
            return False
        except Exception as e:
            print(f"❌ WhatsApp failed: {e}")
            return False
    
    def send_facebook_message(self, recipient_id: str, message: str, page_token: str) -> bool:
        """
        Send Facebook Messenger message
        
        Args:
            recipient_id: Facebook PSID or page-scoped ID
            message: Message content
            page_token: Facebook Page Access Token
        """
        if self.simulation_mode or not page_token:
            print(f"[SIMULATED FACEBOOK] To: {recipient_id} | Message: {message[:50]}...")
            return True
        
        try:
            url = "https://graph.facebook.com/v18.0/me/messages"
            payload = {
                "recipient": {"id": recipient_id},
                "message": {"text": message},
                "access_token": page_token
            }
            
            response = requests.post(url, json=payload)
            data = response.json()
            
            return 'message_id' in data
            
        except Exception as e:
            print(f"❌ Facebook Messenger failed: {e}")
            return False
    
    def send_facebook_comment(self, post_id: str, message: str, page_token: str) -> bool:
        """Reply to a Facebook post comment"""
        try:
            url = f"https://graph.facebook.com/v18.0/{post_id}/comments"
            params = {
                "message": message,
                "access_token": page_token
            }
            
            response = requests.post(url, params=params)
            data = response.json()
            
            return 'id' in data
            
        except Exception as e:
            print(f"❌ Facebook comment failed: {e}")
            return False
    
    def generate_whatsapp_link(self, phone: str, message: str) -> str:
        """Generate a WhatsApp click-to-chat link"""
        # Clean phone number
        phone = re.sub(r'\D', '', phone)
        encoded_message = urllib.parse.quote(message)
        return f"https://wa.me/{phone}?text={encoded_message}"
    
    def generate_messenger_link(self, facebook_username: str, message: str) -> str:
        """Generate a Messenger link"""
        encoded_message = urllib.parse.quote(message)
        return f"https://m.me/{facebook_username}?text={encoded_message}"
    
    def send_campaign_message(self, lead: Lead, campaign: Campaign, channel: str, user_settings: User) -> Optional[MessageRecord]:
        """Send a message via specified channel"""
        
        message_id = f"msg_{int(time.time())}_{lead.lead_id}_{channel}"
        timestamp = datetime.datetime.now().isoformat()
        
        # Prepare content based on channel
        if channel == ChannelType.EMAIL.value:
            content = campaign.email_body
            subject = campaign.email_subject
            
            # Replace placeholders
            content = content.replace("[Name]", lead.name)
            content = content.replace("[Company]", lead.company)
            content = content.replace("[Industry]", lead.industry or "")
            content = content.replace("[Location]", lead.location or "")
            rating = str(lead.rating) if lead.rating and lead.rating > 0 else ""
            content = content.replace("[Rating]", rating)
            
            subject = subject.replace("[Name]", lead.name)
            subject = subject.replace("[Company]", lead.company)
            
            # For email, combine subject and body
            full_content = f"Subject: {subject}\n\n{content}"
            
            # Send email
            success = self.send_email(
                lead.email, 
                subject, 
                content, 
                from_name=user_settings.sender_name or "Copywriter Pro"
            )
            
        elif channel == ChannelType.WHATSAPP.value:
            content = campaign.whatsapp_template
            
            # Replace placeholders
            content = content.replace("[Name]", lead.name.split()[0])  # First name only
            content = content.replace("[Company]", lead.company)
            content = content.replace("[Industry]", lead.industry or "")
            rating = str(lead.rating) if lead.rating and lead.rating > 0 else ""
            content = content.replace("[Rating]", rating)
            
            full_content = content
            
            # Prepare Twilio settings
            twilio_settings = {
                'account_sid': user_settings.twilio_account_sid,
                'auth_token': user_settings.twilio_auth_token,
                'from_number': user_settings.twilio_whatsapp_number
            }
            
            # Send WhatsApp
            success = self.send_whatsapp(lead.phone, content, twilio_settings)
            
        elif channel == ChannelType.FACEBOOK.value:
            content = campaign.facebook_template
            
            # Replace placeholders
            content = content.replace("[Name]", lead.name.split()[0])
            content = content.replace("[Company]", lead.company)
            content = content.replace("[Industry]", lead.industry or "")
            rating = str(lead.rating) if lead.rating and lead.rating > 0 else ""
            content = content.replace("[Rating]", rating)
            
            full_content = content
            
            # Determine recipient ID
            recipient_id = lead.facebook_id or lead.facebook_url.split('/')[-1] if lead.facebook_url else None
            
            if recipient_id:
                success = self.send_facebook_message(
                    recipient_id, 
                    content, 
                    user_settings.facebook_page_token
                )
            else:
                print(f"❌ No Facebook recipient ID for lead {lead.lead_id}")
                success = False
        else:
            return None
        
        # Create message record
        message = MessageRecord(
            message_id=message_id,
            lead_id=lead.lead_id,
            campaign_id=campaign.campaign_id,
            user_id=lead.user_id,
            channel=channel,
            content=full_content,
            sent_at=timestamp,
            status=MessageStatus.SENT.value if success else MessageStatus.FAILED.value,
            error_message=None if success else f"Failed to send via {channel}"
        )
        
        # Update lead's last contacted
        if success:
            lead.last_contacted = timestamp
            lead.contact_attempts += 1
            lead.preferred_channel = channel  # Set as preferred if successful
        
        return message

# ============================================================================
# BUSINESS DISCOVERY
# ============================================================================

class BusinessDiscovery:
    def __init__(self):
        self.google_places = None
    
    def _init_google_places(self, api_key: str):
        if not self.google_places:
            self.google_places = GooglePlacesDiscovery(api_key)
        else:
            self.google_places.set_api_key(api_key)
        return self.google_places.authenticated
    
    def discover_businesses(self, campaign: Campaign, user_api_key: str = None, max_businesses: int = 50) -> List[Dict]:
        """Discover businesses using Google Places API"""
        if not campaign.search_queries:
            print("❌ No search queries defined")
            return []
        
        if not user_api_key:
            print("❌ Google Places API key missing")
            return []
        
        if not self._init_google_places(user_api_key):
            print("❌ Google Places API authentication failed")
            return []
        
        all_businesses = []
        seen_place_ids = set()
        
        try:
            for query in campaign.search_queries[:3]:
                locations = campaign.search_locations if campaign.search_locations else ['']
                
                for location in locations[:3]:
                    if not location and not query:
                        continue
                    
                    print(f"\n🔍 Searching: '{query}' in '{location or 'any location'}'")
                    
                    remaining = max_businesses - len(all_businesses)
                    if remaining <= 0:
                        break
                    
                    per_search = min(
                        campaign.max_results_per_search or 20,
                        remaining,
                        20
                    )
                    
                    businesses = self.google_places.search_places(
                        query=query,
                        location=location,
                        max_results=per_search
                    )
                    
                    for biz in businesses:
                        place_id = biz.get('place_id')
                        if place_id and place_id not in seen_place_ids:
                            seen_place_ids.add(place_id)
                            
                            if campaign.min_rating > 0 and biz.get('rating', 0) < campaign.min_rating:
                                continue
                            
                            if not biz.get('industry'):
                                biz['industry'] = query
                            
                            all_businesses.append(biz)
                            
                            if len(all_businesses) >= max_businesses:
                                break
                    
                    time.sleep(1)
                    
                if len(all_businesses) >= max_businesses:
                    break
            
            print(f"\n✅ Total unique businesses found: {len(all_businesses)}")
            
        except Exception as e:
            print(f"❌ Discovery error: {e}")
            import traceback
            traceback.print_exc()
        
        return all_businesses[:max_businesses]
    
    def quick_search(self, query: str, location: str, api_key: str, max_results: int = 20) -> List[Dict]:
        """Quick one-off search"""
        if not self._init_google_places(api_key):
            return []
        
        return self.google_places.search_places(
            query=query,
            location=location,
            max_results=max_results
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
            phone = row.get('phone', row.get('Phone', row.get('whatsapp', '')) or '').strip()
            phone = re.sub(r'\D', '', phone)  # Clean phone number
            
            lead = Lead(
                lead_id=f"lead_{int(time.time())}_{i}_{random.randint(1000,9999)}",
                campaign_id=campaign_id,
                user_id=user_id,
                name=row.get('name', row.get('Name', 'Unknown')) or 'Unknown',
                company=row.get('company', row.get('Company', '')) or '',
                email=email,
                phone=phone,
                facebook_url=row.get('facebook', row.get('Facebook', '')) or '',
                website=row.get('website', row.get('Website', '')) or '',
                industry=row.get('industry', row.get('Industry', '')) or '',
                location=row.get('location', row.get('Location', '')) or '',
                country=row.get('country', row.get('Country', '')) or '',
                timezone=row.get('timezone', row.get('Timezone', '')) or '',
                notes=row.get('notes', row.get('Notes', '')) or '',
                linkedin_url=row.get('linkedin', row.get('LinkedIn', '')) or '',
                job_title=row.get('job_title', row.get('Job Title', '')) or '',
                source=LeadSource.CSV.value,
                status=LeadStatus.PENDING.value,
                created_at=datetime.datetime.now().isoformat(),
                updated_at=datetime.datetime.now().isoformat()
            )
            leads.append(lead)
        return leads

    @staticmethod
    def score_lead(lead: Lead, campaign: Campaign) -> int:
        score = 0
        
        # Industry match
        if campaign.ideal_industries and lead.industry:
            if any(i.lower() in lead.industry.lower() for i in campaign.ideal_industries):
                score += 30
        
        # Location match
        if campaign.search_locations and lead.location:
            if any(l.lower() in lead.location.lower() for l in campaign.search_locations):
                score += 20
        
        # Rating score (handle None)
        rating = lead.rating if lead.rating is not None else 0
        if rating >= 4.5:
            score += 25
        elif rating >= 4.0:
            score += 15
        elif rating >= 3.5:
            score += 10
        
        # Contact availability
        if lead.email:
            score += 20
        if lead.phone:
            score += 15
        if lead.facebook_url:
            score += 10
        if lead.website:
            score += 10
        
        # Business status
        if lead.business_status == 'OPERATIONAL':
            score += 10
        
        return min(100, score)

# ============================================================================
# ANALYTICS ENGINE - FIXED VERSION
# ============================================================================

class AnalyticsEngine:
    @staticmethod
    def get_campaign_stats(db: Database, user_id: str, campaign_id: str) -> Dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            return {}
        
        leads = db.get_campaign_leads(user_id, campaign_id)
        total = len(leads)
        
        # Safe handling of message stats
        messages = []
        for lead in leads:
            try:
                messages.extend(db.get_lead_messages(lead.lead_id))
            except:
                pass
        
        # Message stats by channel with safe defaults
        channel_stats = {
            'email': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0},
            'whatsapp': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0},
            'facebook': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0}
        }
        
        for msg in messages:
            channel = msg.channel if hasattr(msg, 'channel') and msg.channel else 'email'
            if channel not in channel_stats:
                channel = 'email'
            
            if msg.status == MessageStatus.SENT.value:
                channel_stats[channel]['sent'] += 1
            elif msg.status == MessageStatus.FAILED.value:
                channel_stats[channel]['failed'] += 1
            
            if hasattr(msg, 'read_at') and msg.read_at:
                channel_stats[channel]['read'] += 1
            if hasattr(msg, 'replied_at') and msg.replied_at:
                channel_stats[channel]['replied'] += 1
        
        # Lead stats
        hot = len([l for l in leads if l.status == LeadStatus.QUALIFIED_HOT.value])
        cold = len([l for l in leads if l.status == LeadStatus.COLD.value])
        
        # Contact availability (handle None values)
        with_email = len([l for l in leads if l.email and str(l.email).strip()])
        with_phone = len([l for l in leads if l.phone and str(l.phone).strip()])
        with_facebook = len([l for l in leads if l.facebook_url and str(l.facebook_url).strip()])
        with_website = len([l for l in leads if l.website and str(l.website).strip()])
        
        # Average rating - FIXED: Handle None values
        valid_ratings = []
        for l in leads:
            rating = l.rating
            if rating is not None and isinstance(rating, (int, float)) and rating > 0:
                valid_ratings.append(float(rating))
        
        avg_rating = sum(valid_ratings) / len(valid_ratings) if valid_ratings else 0

        # Country breakdown
        countries = {}
        for l in leads:
            if l.country and str(l.country).strip():
                country = str(l.country).strip()
                countries[country] = countries.get(country, 0) + 1
        
        # Sort countries by count and take top 5
        sorted_countries = sorted(countries.items(), key=lambda x: x[1], reverse=True)[:5]
        country_str = "\n".join(f"{c}: {v}" for c, v in sorted_countries)

        # Calculate total sent messages
        total_sent = sum(
            channel_stats['email']['sent'] +
            channel_stats['whatsapp']['sent'] +
            channel_stats['facebook']['sent']
        )

        return {
            'campaign_name': campaign.name,
            'total_leads': total,
            'hot_leads': hot,
            'cold_leads': cold,
            'avg_rating': round(avg_rating, 1),
            'total_sent': total_sent,
            'countries_found': len(countries),
            'country_breakdown': country_str,
            'contact_availability': {
                'email': with_email,
                'phone': with_phone,
                'facebook': with_facebook,
                'website': with_website
            },
            'channel_stats': channel_stats,
            'total_messages': len(messages),
            'total_failed': sum(
                channel_stats['email']['failed'] +
                channel_stats['whatsapp']['failed'] +
                channel_stats['facebook']['failed']
            )
        }

# ============================================================================
# FLASK APP
# ============================================================================

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(16))
CORS(app)

db = Database()
message_service = MessageService()
business_discovery = BusinessDiscovery()
analytics = AnalyticsEngine()

# ============================================================================
# AUTH ROUTES
# ============================================================================

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

# ============================================================================
# SETTINGS ROUTES
# ============================================================================

@app.route('/settings/api', methods=['GET', 'POST'])
def api_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))

    user = db.get_user(session['user_id'])
    if request.method == 'POST':
        api_key = request.form.get('google_places_api_key', '').strip()
        if api_key:
            db.update_user(user.user_id, google_places_api_key=api_key)
            flash('Google Places API key saved!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('api_settings.html', user=user)

@app.route('/settings/email', methods=['GET', 'POST'])
def email_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if request.method == 'POST':
        db.update_user(
            user.user_id,
            email_host=request.form.get('email_host', 'smtp.gmail.com'),
            email_user=request.form.get('email_user'),
            email_password=request.form.get('email_password'),
            sender_name=request.form.get('sender_name')
        )
        flash('Email settings saved!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('email_settings.html', user=user)

@app.route('/settings/whatsapp', methods=['GET', 'POST'])
def whatsapp_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if request.method == 'POST':
        db.update_user(
            user.user_id,
            twilio_account_sid=request.form.get('twilio_account_sid'),
            twilio_auth_token=request.form.get('twilio_auth_token'),
            twilio_whatsapp_number=request.form.get('twilio_whatsapp_number')
        )
        flash('WhatsApp settings saved!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('whatsapp_settings.html', user=user)

@app.route('/settings/facebook', methods=['GET', 'POST'])
def facebook_settings():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if request.method == 'POST':
        db.update_user(
            user.user_id,
            facebook_page_id=request.form.get('facebook_page_id'),
            facebook_page_token=request.form.get('facebook_page_token')
        )
        flash('Facebook settings saved!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('facebook_settings.html', user=user)

# ============================================================================
# DASHBOARD
# ============================================================================

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    campaigns = db.get_user_campaigns(session['user_id'])
    stats = []
    
    for c in campaigns:
        try:
            campaign_stats = analytics.get_campaign_stats(db, session['user_id'], c.campaign_id)
            stats.append(campaign_stats)
        except Exception as e:
            print(f"Error getting stats for campaign {c.campaign_id}: {e}")
            # Append empty stats to maintain order
            stats.append({
                'total_leads': 0,
                'emails_sent': 0,
                'hot_leads': 0,
                'avg_rating': 0,
                'campaign_name': c.name
            })
    
    return render_template('dashboard.html', campaigns=campaigns, stats=stats, user=user)

# ============================================================================
# CAMPAIGN MANAGEMENT
# ============================================================================

@app.route('/campaign/new', methods=['GET', 'POST'])
def new_campaign():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        # Get search queries
        search_queries = [q.strip() for q in request.form.get('search_queries', '').split(',') if q.strip()]
        search_locations = [l.strip() for l in request.form.get('search_locations', '').split(',') if l.strip()]
        ideal_industries = [i.strip() for i in request.form.get('ideal_industries', '').split(',') if i.strip()]
        
        # Get enabled channels
        channels_enabled = request.form.getlist('channels_enabled')
        
        try:
            max_results_per_search = int(request.form.get('max_results_per_search', 20))
            min_rating = float(request.form.get('min_rating', 0))
        except:
            max_results_per_search = 20
            min_rating = 0
        
        campaign = Campaign(
            campaign_id=f"camp_{int(time.time())}",
            user_id=session['user_id'],
            name=request.form.get('name'),
            created_at=datetime.datetime.now().isoformat(),
            search_queries=search_queries,
            search_locations=search_locations,
            max_results_per_search=max_results_per_search,
            ideal_industries=ideal_industries,
            min_rating=min_rating,
            channels_enabled=channels_enabled,
            email_subject=request.form.get('email_subject'),
            email_body=request.form.get('email_body'),
            whatsapp_template=request.form.get('whatsapp_template'),
            whatsapp_enabled='whatsapp' in channels_enabled,
            facebook_template=request.form.get('facebook_template'),
            facebook_enabled='facebook' in channels_enabled,
            notify_email=request.form.get('notify_email')
        )
        
        db.save_campaign(session['user_id'], campaign)
        flash('Campaign created!', 'success')
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

@app.route('/campaign/<campaign_id>/discover-businesses', methods=['POST'])
def discover_businesses_route(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    campaign = db.get_campaign(campaign_id)
    user = db.get_user(session['user_id'])
    api_key = user.google_places_api_key if user else None

    if not api_key:
        flash('Please add your Google Places API key first!', 'error')
        return redirect(url_for('api_settings'))

    flash('Starting business discovery...', 'success')

    def discover(uid, cid, campaign, api_key):
        discovered = business_discovery.discover_businesses(
            campaign, 
            user_api_key=api_key, 
            max_businesses=campaign.max_results or 50
        )
        
        leads = []
        for i, biz in enumerate(discovered):
            # Ensure rating is not None
            rating = biz.get('rating', 0)
            if rating is None:
                rating = 0
                
            lead = Lead(
                lead_id=f"lead_{int(time.time())}_{i}_{random.randint(1000,9999)}",
                campaign_id=cid,
                user_id=uid,
                name=biz.get('name', 'Contact'),
                company=biz.get('company', biz.get('name', 'Unknown')),
                email=biz.get('email', ''),
                phone=biz.get('phone', ''),
                facebook_url=biz.get('facebook_url', ''),
                website=biz.get('website', ''),
                industry=biz.get('industry', campaign.search_queries[0] if campaign.search_queries else ''),
                location=biz.get('location', ''),
                country=biz.get('country', ''),
                place_id=biz.get('place_id', ''),
                rating=rating,
                total_ratings=biz.get('total_ratings', 0),
                price_level=biz.get('price_level', 0),
                business_status=biz.get('business_status', ''),
                types=biz.get('types', ''),
                source=biz.get('source', LeadSource.GOOGLE_PLACES.value),
                status=LeadStatus.PENDING.value,
                created_at=datetime.datetime.now().isoformat(),
                updated_at=datetime.datetime.now().isoformat()
            )
            
            lead.qualification_score = LeadProcessor.score_lead(lead, campaign)
            leads.append(lead)
        
        db.save_leads(uid, cid, leads)
        print(f"✅ Discovered {len(leads)} leads")
        flash(f'Discovery complete! Found {len(leads)} new leads.', 'success')

    Thread(target=discover, args=(session['user_id'], campaign_id, campaign, api_key)).start()
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

@app.route('/campaign/<campaign_id>/send-messages', methods=['POST'])
def send_messages(campaign_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    campaign = db.get_campaign(campaign_id)
    user = db.get_user(session['user_id'])
    channel = request.form.get('channel', 'email')
    
    if not campaign:
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
    
    leads = db.get_leads_by_channel(session['user_id'], campaign_id, channel, 50)
    if not leads:
        flash(f'No leads with {channel} contact info', 'info')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))

    for lead in leads:
        lead.contact_attempts += 1
        db.update_lead(lead)

    flash(f'Started sending {len(leads)} messages via {channel} in background', 'success')

    def send(uid, cid, campaign, leads, channel, user):
        for lead in leads:
            try:
                message = message_service.send_campaign_message(lead, campaign, channel, user)
                if message:
                    db.save_message(uid, message)
                    lead.last_contacted = message.sent_at
                    db.update_lead(lead)
                time.sleep(random.uniform(1, 3))  # Random delay between messages
            except Exception as e:
                print(f"Error sending message to {lead.lead_id}: {e}")

    Thread(target=send, args=(session['user_id'], campaign_id, campaign, leads, channel, user)).start()
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))

# ============================================================================
# MANUAL SEARCH
# ============================================================================

@app.route('/search', methods=['GET', 'POST'])
def manual_search():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    user = db.get_user(session['user_id'])
    if not user or not user.google_places_api_key:
        flash('Please add your Google Places API key first', 'error')
        return redirect(url_for('api_settings'))
    
    campaigns = db.get_user_campaigns(session['user_id'])
    results = []
    
    if request.method == 'POST':
        query = request.form.get('query', '')
        location = request.form.get('location', '')
        
        try:
            max_results = int(request.form.get('max_results', 20))
        except:
            max_results = 20
        
        if query:
            flash(f'Searching for "{query}"...', 'info')
            discovery = GooglePlacesDiscovery(user.google_places_api_key)
            results = discovery.search_places(
                query=query,
                location=location,
                max_results=max_results
            )
            flash(f'Found {len(results)} businesses', 'success')
    
    return render_template('search.html', results=results, campaigns=campaigns)

@app.route('/search/save-to-campaign', methods=['POST'])
def save_search_to_campaign():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    campaign_id = data.get('campaign_id')
    businesses = data.get('businesses', [])
    
    if not campaign_id or not businesses:
        return jsonify({'error': 'Missing data'}), 400
    
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        return jsonify({'error': 'Campaign not found'}), 404
    
    leads = []
    for i, biz in enumerate(businesses):
        rating = biz.get('rating', 0)
        if rating is None:
            rating = 0
            
        lead = Lead(
            lead_id=f"lead_{int(time.time())}_{i}_{random.randint(1000,9999)}",
            campaign_id=campaign_id,
            user_id=session['user_id'],
            name=biz.get('name', 'Contact'),
            company=biz.get('company', biz.get('name', 'Unknown')),
            email='',
            phone=biz.get('phone', ''),
            facebook_url=biz.get('facebook_url', ''),
            website=biz.get('website', ''),
            industry=biz.get('industry', ''),
            location=biz.get('location', ''),
            country=biz.get('country', ''),
            place_id=biz.get('place_id', ''),
            rating=rating,
            total_ratings=biz.get('total_ratings', 0),
            source=LeadSource.MANUAL_SEARCH.value,
            status=LeadStatus.PENDING.value,
            created_at=datetime.datetime.now().isoformat(),
            updated_at=datetime.datetime.now().isoformat()
        )
        lead.qualification_score = LeadProcessor.score_lead(lead, campaign)
        leads.append(lead)
    
    db.save_leads(session['user_id'], campaign_id, leads)
    
    return jsonify({
        'success': True,
        'count': len(leads),
        'message': f'Saved {len(leads)} leads to campaign'
    })

# ============================================================================
# LEAD ROUTES
# ============================================================================

@app.route('/lead/<lead_id>')
def lead_detail(lead_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    lead = db.get_lead(lead_id)
    if not lead:
        flash('Lead not found', 'error')
        return redirect(url_for('dashboard'))
    
    messages = db.get_lead_messages(lead_id)
    campaign = db.get_campaign(lead.campaign_id)
    user = db.get_user(session['user_id'])
    
    # Generate WhatsApp link if phone exists
    whatsapp_link = None
    if lead.phone and campaign and campaign.whatsapp_template:
        message = campaign.whatsapp_template.replace("[Name]", lead.name.split()[0])
        message = message.replace("[Company]", lead.company)
        whatsapp_link = message_service.generate_whatsapp_link(lead.phone, message)
    
    return render_template('lead_detail.html', lead=lead, messages=messages, 
                          campaign=campaign, whatsapp_link=whatsapp_link)

@app.route('/lead/<lead_id>/send-message', methods=['POST'])
def send_lead_message(lead_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    
    campaign = db.get_campaign(lead.campaign_id)
    user = db.get_user(session['user_id'])
    channel = request.json.get('channel', 'email')
    
    message = message_service.send_campaign_message(lead, campaign, channel, user)
    
    if message:
        db.save_message(session['user_id'], message)
        lead.last_contacted = message.sent_at
        lead.contact_attempts += 1
        db.update_lead(lead)
        
        return jsonify({
            'status': message.status,
            'message_id': message.message_id,
            'channel': channel
        })
    else:
        return jsonify({'error': f'Failed to send via {channel}'}), 500

# ============================================================================
# ANALYTICS
# ============================================================================

@app.route('/analytics')
def analytics_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    campaigns = db.get_user_campaigns(session['user_id'])
    all_stats = []
    
    for c in campaigns:
        try:
            stats = analytics.get_campaign_stats(db, session['user_id'], c.campaign_id)
            all_stats.append(stats)
        except Exception as e:
            print(f"Error getting analytics for {c.campaign_id}: {e}")
            # Append empty stats
            all_stats.append({
                'campaign_name': c.name,
                'total_leads': 0,
                'hot_leads': 0,
                'total_sent': 0,
                'avg_rating': 0,
                'channel_stats': {
                    'email': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0},
                    'whatsapp': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0},
                    'facebook': {'sent': 0, 'failed': 0, 'read': 0, 'replied': 0}
                }
            })
    
    # Aggregate stats
    total_leads = sum(s.get('total_leads', 0) for s in all_stats)
    total_hot = sum(s.get('hot_leads', 0) for s in all_stats)
    total_messages = sum(s.get('total_sent', 0) for s in all_stats)
    
    # Channel breakdown
    channel_totals = {
        'email': sum(s.get('channel_stats', {}).get('email', {}).get('sent', 0) for s in all_stats),
        'whatsapp': sum(s.get('channel_stats', {}).get('whatsapp', {}).get('sent', 0) for s in all_stats),
        'facebook': sum(s.get('channel_stats', {}).get('facebook', {}).get('sent', 0) for s in all_stats)
    }
    
    return render_template('analytics.html', 
                          stats=all_stats, 
                          total_leads=total_leads,
                          total_hot=total_hot,
                          total_messages=total_messages,
                          channel_totals=channel_totals,
                          total_campaigns=len(campaigns))

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route('/health', methods=['GET', 'HEAD'])
def health_check():
    return '', 200

# ============================================================================
# MAIN
# ============================================================================

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
    print("="*70)
    print(" FREELANCE COPYWRITER CLIENT ACQUISITION SYSTEM - MULTI-CHANNEL")
    print("="*70)
    print("Powered by:")
    print("  • Google Places API - Find businesses")
    print("  • Email - Send campaigns via SMTP")
    print("  • WhatsApp - Send messages via Twilio")
    print("  • Facebook - Send messages via Messenger")
    print("="*70)
    
    create_default_user()
    
    print("\n✅ System ready!")
    print("🔗 http://localhost:5000")
    print("👤 Login: admin / admin123")
    print("="*70)
    
    if not os.environ.get('RENDER'):
        Thread(target=open_browser).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)