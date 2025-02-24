import smtplib
from email.message import EmailMessage

from backend.settings import backend_settings
from utils.logger import logger_setup


class EmailNotifier:
    def __init__(self):
        self.sender_email = backend_settings.get("SENDER_EMAIL")
        self.sender_email_password = backend_settings.get("EMAIL_PASSWORD")
        self.email_receivers = backend_settings.get("EMAIL_RECEIVERS")
        self.logger = logger_setup("backend")
        self.smtp = "smtp.gmail.com"
        self.port = 465

    def create_message(self, message: str):
        msg = EmailMessage()
        msg.set_content(message, subtype="html")
        msg["Subject"] = f"Данные по ГРС"
        msg["From"] = self.sender_email
        msg["To"] = ", ".join(self.email_receivers)
        return msg

    def send_message(self, message: str):
        msg = self.create_message(message=message)
        try:
            with smtplib.SMTP_SSL(self.smtp, self.port) as server:
                server.login(self.sender_email, self.sender_email_password)
                server.send_message(msg)
        except Exception as e:
            self.logger.error(f"Failed to send email: {e}")
