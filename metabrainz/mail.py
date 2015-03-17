# -*- coding: utf-8 -*-
"""
This module provides different ways to send emails.
"""
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import current_app
import smtplib


def send_mail(subject, text, recipients, attachments=None,
              from_name="MetaBrainz Notifications",
              from_addr='noreply@'+current_app.config['MAIL_FROM_DOMAIN']):
    """This function can be used as a foundation for sending email.

    Args:
        subject: Subject of the message.
        message: The message itself.
        recipients: List of recipients.
        attachments: List of (file object, subtype, name) tuples. For example:
            (<file_obj>, 'pdf', 'receipt.pdf').
        from_name: Name of the sender.
        from_addr: Email address of the sender.
    """
    if attachments is None:
        attachments = []

    if current_app.config['TESTING']:  # Not sending any emails during the testing process
        return

    message = MIMEMultipart('mixed')
    message['Subject'] = subject
    message['From'] = "%s <%s>" % (from_name, from_addr)
    message.attach(MIMEText(text, _charset='utf-8'))

    for attachment in attachments:
        file_obj, subtype, name = attachment
        attachment = MIMEApplication(file_obj.read(), _subtype=subtype)
        file_obj.close()  # FIXME(roman): This feels kind of hacky. Maybe there's a better way?
        attachment.add_header('content-disposition', 'attachment', filename=name)
        message.attach(attachment)

    smtp_server = smtplib.SMTP(current_app.config['SMTP_SERVER'], current_app.config['SMTP_PORT'])
    smtp_server.sendmail(from_addr, recipients, message.as_string())
    smtp_server.quit()