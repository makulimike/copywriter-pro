// Stripe Checkout
document.addEventListener('DOMContentLoaded', function() {
    const checkoutButton = document.getElementById('checkout-button');
    
    if (checkoutButton) {
        checkoutButton.addEventListener('click', async function(e) {
            e.preventDefault();
            window.location.href = '/webhook/stripe-test'; // You'll implement this
        });
    }
    
    // Dashboard Stats
    const dashboard = document.querySelector('.dashboard');
    if (dashboard) {
        fetchDashboardStats();
    }
    
    // Smooth scrolling
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const target = document.querySelector(this.getAttribute('href'));
            if (target) {
                target.scrollIntoView({ behavior: 'smooth' });
            }
        });
    });
});

async function fetchDashboardStats() {
    try {
        const response = await fetch('/api/user/stats');
        const stats = await response.json();
        
        document.getElementById('total-calls').textContent = stats.total_calls;
        document.getElementById('hot-leads').textContent = stats.hot_leads;
        document.getElementById('booked-calls').textContent = stats.booked_calls;
        document.getElementById('conversion-rate').textContent = stats.conversion_rate;
    } catch (error) {
        console.error('Error fetching stats:', error);
    }
}

// Campaign Setup Form
const campaignForm = document.getElementById('campaign-form');
if (campaignForm) {
    campaignForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        
        const formData = {
            name: document.getElementById('campaign-name').value,
            niche: document.getElementById('target-niche').value,
            script: document.getElementById('script').value
        };
        
        try {
            const response = await fetch('/api/campaign/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData)
            });
            
            const data = await response.json();
            if (data.status === 'success') {
                window.location.href = '/add-leads?campaign=' + data.campaign_id;
            }
        } catch (error) {
            console.error('Error creating campaign:', error);
        }
    });
}

// Add Leads Form
const leadsForm = document.getElementById('leads-form');
if (leadsForm) {
    leadsForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        
        const urlParams = new URLSearchParams(window.location.search);
        const campaignId = urlParams.get('campaign');
        
        // Simple CSV parsing (you'll want to enhance this)
        const leadsText = document.getElementById('leads-csv').value;
        const leads = leadsText.split('\n').map(line => {
            const [name, phone] = line.split(',');
            return { name: name?.trim(), phone: phone?.trim() };
        }).filter(lead => lead.name && lead.phone);
        
        try {
            const response = await fetch('/api/leads/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ campaignId, leads })
            });
            
            const data = await response.json();
            if (data.status === 'success') {
                await launchCampaign(campaignId);
            }
        } catch (error) {
            console.error('Error uploading leads:', error);
        }
    });
}

async function launchCampaign(campaignId) {
    try {
        const response = await fetch(`/api/campaign/${campaignId}/launch`, {
            method: 'POST'
        });
        const data = await response.json();
        if (data.status === 'started') {
            window.location.href = '/dashboard';
        }
    } catch (error) {
        console.error('Error launching campaign:', error);
    }
}