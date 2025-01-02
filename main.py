import os
import imaplib
import email
from email.header import decode_header
import ssl
import smtplib
from email.mime.text import MIMEText
import logging
import io
import uuid
import re
import requests
from flask import Flask, render_template
from flask import jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import threading
import time
import pytz

app = Flask(__name__)

# Configure SQLAlchemy
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'weather_emails.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Weather API Configuration
WEATHERAPI_KEY = '31b4cb0b06d148ffad870619241312'

# Email Configuration
EMAIL_ADDRESS = 'charlsweather@gmail.com'
EMAIL_PASSWORD = 'swuo dtov lqru ctsx'
IMAP_SERVER = 'imap.gmail.com'
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 465

# Configure logging
class LogCapture:
    def __init__(self):
        self.log_output = io.StringIO()
        self.logger = logging.getLogger('weather_email_logger')
        self.logger.setLevel(logging.DEBUG)
        
        self.stream_handler = logging.StreamHandler(self.log_output)
        self.stream_handler.setLevel(logging.DEBUG)
        
        formatter = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')
        self.stream_handler.setFormatter(formatter)
        
        self.logger.addHandler(self.stream_handler)
    
    def get_logs(self):
        self.log_output.seek(0)
        logs = self.log_output.read()
        self.log_output.truncate(0)
        self.log_output.seek(0)
        return logs

log_capture = LogCapture()

# Email Request Model
class WeatherRequest(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    requester_email = db.Column(db.String(255), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    region = db.Column(db.String(100))
    country = db.Column(db.String(100))
    received_at = db.Column(db.DateTime, default=datetime.utcnow)
    processed = db.Column(db.Boolean, default=False)

# Weather API Configuration
def get_weather_data(city, country=None):
    """
    Fetch weather data from WeatherAPI
    """
    try:
        # Construct the query parameter
        query = f"{city},{country}" if country else city
        
        url = f"http://api.weatherapi.com/v1/current.json?key={WEATHERAPI_KEY}&q={query}&aqi=no"
        
        response = requests.get(url)
        response.raise_for_status()
        
        weather_data = response.json()
        
        # Grab the data
        return {
            "city": weather_data['location']['name'],
            "country": weather_data['location']['country'],
            "temperature": weather_data['current']['temp_c'],
            "feels_like": weather_data['current']['feelslike_c'],
            "humidity": weather_data['current']['humidity'],
            "description": weather_data['current']['condition']['text'],
            "wind_speed": weather_data['current']['wind_kph'],
            "pressure": weather_data['current']['pressure_mb']
        }
    except requests.RequestException as e:
        log_capture.logger.error(f"Weather API Error: {str(e)}")
        return None


def send_weather_response(requester_email, weather_data):
    """
    Send weather response email
    """
    try:
        msg = MIMEText(f"""Weather Report for {weather_data['city']}, {weather_data['country']}:

Temperature: {weather_data['temperature']}°C
Feels Like: {weather_data['feels_like']}°C
Humidity: {weather_data['humidity']}%
Description: {weather_data['description']}
Wind Speed: {weather_data['wind_speed']} m/s
Atmospheric Pressure: {weather_data['pressure']} hPa
""")
        
        msg['Subject'] = f"Weather Report for {weather_data['city']}"
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = requester_email

        # Send via SMTP
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, context=context) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_ADDRESS, requester_email, msg.as_string())
            
            print(weather_data)
        
        log_capture.logger.info(f"Weather response sent to {requester_email}")
    except Exception as e:
        log_capture.logger.error(f"Error sending weather response: {str(e)}")

def parse_email_request(body):
    """
    Parse email body for city, region, country
    Expected format: "city, region, country"
    """
    parts = [part.strip() for part in body.split(',')]
    
    # Handle different input formats
    if len(parts) == 1:
        return parts[0], None, None
    elif len(parts) == 2:
        return parts[0], None, parts[1]
    elif len(parts) == 3:
        return parts[0], parts[1], parts[2]
    else:
        return None, None, None

def start_email_listener():
    """
    Continuously listen for new email requests
    """
    while True:
        try:
            # Connect to IMAP server
            mail = imaplib.IMAP4_SSL(IMAP_SERVER)
            mail.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            mail.select('inbox')

            log_capture.logger.info("Connected to IMAP server. Searching for new emails...")

            # Search for unseen emails
            _, search_data = mail.search(None, 'UNSEEN')
            
            for num in search_data[0].split():
                # Fetch the email message by ID
                _, msg_data = mail.fetch(num, '(RFC822)')
                
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        # Parse the email
                        msg = email.message_from_bytes(response_part[1])
                        
                        # Get sender email
                        sender = msg['From']
                        
                        # Extract email body
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == 'text/plain':
                                    body = part.get_payload(decode=True).decode()
                                    break
                        else:
                            body = msg.get_payload(decode=True).decode()
                        
                        # Parse city information
                        city, region, country = parse_email_request(body)
                        
                        if city:
                            # Create database entry
                            with app.app_context():
                                # Create weather request record
                                weather_request = WeatherRequest(
                                    id=str(uuid.uuid4()),
                                    requester_email=sender,
                                    city=city,
                                    region=region,
                                    country=country
                                )
                                db.session.add(weather_request)
                                db.session.commit()
                                
                                # Fetch weather data
                                weather_data = get_weather_data(city, country)
                                
                                if weather_data:
                                    # Send response email
                                    send_weather_response(sender, weather_data)
                                    
                                    # Mark request as processed
                                    weather_request.processed = True
                                    db.session.commit()
                                
                                log_capture.logger.info(f"Processed weather request for {city}")
                                print(f"Processed Request for: {city}")
                
                # Mark email as seen
                mail.store(num, '+FLAGS', '\\Seen')
            
            # Wait before checking again
            time.sleep(15)
        
        except Exception as e:
            log_capture.logger.error(f"Email listener error: {str(e)}")
            time.sleep(60)  # Wait a minute before retrying

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/get_logs")
def get_logs():
    return log_capture.get_logs()

@app.route("/get_weather_requests")
def get_weather_requests():
    """
    Fetch all weather requests from the database
    """
    with app.app_context():
        # Fetch all requests, ordered by most recent first
        requests = WeatherRequest.query.order_by(WeatherRequest.received_at.desc()).all()
        
        # Convert to list of dictionaries for JSON serialization
        request_list = [{
            'id': req.id,
            'requester_email': req.requester_email,
            'city': req.city,
            'region': req.region or 'N/A',
            'country': req.country or 'N/A',
            'received_at': req.received_at.strftime('%Y-%m-%d %H:%M:%S'),
            'processed': req.processed
        } for req in requests]
        
        return jsonify(request_list)

if __name__ == "__main__":
    # Create database tables
    with app.app_context():
        db.create_all()
    
    # Start email listener in a separate thread
    email_listener_thread = threading.Thread(target=start_email_listener, daemon=True)
    email_listener_thread.start()
    
    # Run the Flask app (hosting it on render)
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
    
    # To run the app locally comment or remove lines 275 & 276 and uncomment the code bellow
    #app.run(debug=True)

    
