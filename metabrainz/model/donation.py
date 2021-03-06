from __future__ import division
from metabrainz.model import db
from metabrainz.donations.receipts import send_receipt
from metabrainz.admin import AdminModelView
from sqlalchemy.sql import func, desc
from flask import current_app
from datetime import datetime
from wepay import WePay
import stripe
import logging


PAYMENT_METHOD_STRIPE = 'stripe'
PAYMENT_METHOD_PAYPAL = 'paypal'
PAYMENT_METHOD_WEPAY = 'wepay'
PAYMENT_METHOD_BITCOIN = 'bitcoin'
PAYMENT_METHOD_CHECK = 'check'


class Donation(db.Model):
    __tablename__ = 'donation'

    id = db.Column(db.Integer, primary_key=True)

    # Personal details
    first_name = db.Column(db.Unicode, nullable=False)
    last_name = db.Column(db.Unicode, nullable=False)
    email = db.Column(db.Unicode, nullable=False)
    editor_name = db.Column(db.Unicode)  # MusicBrainz username
    can_contact = db.Column(db.Boolean, nullable=False, default=True)
    anonymous = db.Column(db.Boolean, nullable=False, default=False)
    address_street = db.Column(db.Unicode)
    address_city = db.Column(db.Unicode)
    address_state = db.Column(db.Unicode)
    address_postcode = db.Column(db.Unicode)
    address_country = db.Column(db.Unicode)

    # Transaction details
    payment_date = db.Column(db.DateTime(timezone=True), default=datetime.utcnow)
    payment_method = db.Column(db.Enum(
        PAYMENT_METHOD_STRIPE,
        PAYMENT_METHOD_PAYPAL,
        PAYMENT_METHOD_WEPAY,
        PAYMENT_METHOD_BITCOIN,
        PAYMENT_METHOD_CHECK,
        name='payment_method_types'
    ))
    transaction_id = db.Column(db.Unicode)
    amount = db.Column(db.Numeric(11, 2), nullable=False)
    fee = db.Column(db.Numeric(11, 2))
    memo = db.Column(db.Unicode)

    def __unicode__(self):
        return 'Donation #%s' % self.id

    @classmethod
    def get_by_transaction_id(cls, transaction_id):
        return cls.query.filter_by(transaction_id=str(transaction_id)).first()

    @staticmethod
    def get_nag_days(editor):
        """

        Returns:
            Two values. First one indicates if editor should be nagged:
            -1 = unknown person, 0 = no need to nag, 1 = should be nagged.
            Second is...
        """
        days_per_dollar = 7.5
        result = db.session.execute(
            "SELECT ((amount + COALESCE(fee, 0)) * :days_per_dollar) - "
            "((extract(epoch from now()) - extract(epoch from payment_date)) / 86400) as nag "
            "FROM donation "
            "WHERE lower(editor_name) = lower(:editor) "
            "ORDER BY nag DESC "
            "LIMIT 1",
            {'editor': editor, 'days_per_dollar': days_per_dollar}
        ).fetchone()

        if result is None:
            return -1, 0
        elif result[0] >= 0:
            return 0, result[0]
        else:
            return 1, result[0]

    @classmethod
    def get_recent_donations(cls, limit=None, offset=None):
        """Getter for most recent donations.

        Args:
            limit: Maximum number of donations to be returned.
            offset: Offset of the result.

        Returns:
            Tuple with two items. First is total number if donations. Second
            is a list of donations sorted by payment_date with a specified offset.
        """
        query = cls.query.order_by(cls.payment_date.desc())
        count = query.count()  # Total count should be calculated before limits
        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)
        return count, query.all()

    @classmethod
    def get_biggest_donations(cls, limit=None, offset=None):
        """Getter for biggest donations.

        Donations from the same person are grouped.

        Args:
            limit: Maximum number of donations to be returned.
            offset: Offset of the result.

        Returns:
            Tuple with two items. First is total number if donations. Second
            is a list of donations sorted by amount with a specified offset.
        """
        query = db.session.query(
            cls.first_name.label("first_name"),
            cls.last_name.label("last_name"),
            cls.editor_name.label("editor_name"),
            func.max(cls.payment_date).label("payment_date"),
            func.sum(cls.amount).label("amount"),
            func.sum(cls.fee).label("fee"),
        )
        query = query.filter(cls.anonymous == False)
        query = query.group_by(cls.first_name, cls.last_name, cls.editor_name)
        query = query.order_by(desc("amount"))
        count = query.count()  # Total count should be calculated before limits
        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)
        return count, query.all()

    @classmethod
    def process_paypal_ipn(cls, form):
        """Processor for PayPal IPNs (Instant Payment Notifications).

        Should be used only after IPN request is verified. See PayPal documentation for
        more info about the process.

        Args:
            form: The form parameters from IPN request that contains IPN variables.
                See https://developer.paypal.com/docs/classic/ipn/integration-guide/IPNandPDTVariables/
                for more info about them.
        """
        logging.debug('Processing PayPal IPN...')

        # Only processing completed donations
        if form['payment_status'] != 'Completed':
            logging.info('PayPal: Payment not completed. Status: "%s".', form['payment_status'])
            return

        # We shouldn't process transactions to address for payments
        # TODO: Clarify what this address is
        if form['business'] == current_app.config['PAYPAL_BUSINESS']:
            logging.info('PayPal: Recieved payment to address for payments.')
            return

        if form['receiver_email'] != current_app.config['PAYPAL_PRIMARY_EMAIL']:
            logging.warning('PayPal: Not primary email. Got "%s".', form['receiver_email'])
            return
        if float(form['mc_gross']) < 0.50:
            # Tiny donation
            logging.info('PayPal: Tiny donation ($%s).', form['mc_gross'])
            return

        # Checking that txn_id has not been previously processed
        if cls.get_by_transaction_id(form['txn_id']) is not None:
            logging.info('PayPal: Transaction ID %s has been used before.', form['txn_id'])
            return

        new_donation = cls(
            first_name=form['first_name'],
            last_name=form['last_name'],
            email=form['payer_email'],
            editor_name=form.get('custom'),
            address_street=form.get('address_street'),
            address_city=form.get('address_city'),
            address_state=form.get('address_state'),
            address_postcode=form.get('address_zip'),
            address_country=form.get('address_country'),
            amount=float(form['mc_gross']) - float(form['mc_fee']),
            fee=float(form['mc_fee']),
            transaction_id=form['txn_id'],
            payment_method=PAYMENT_METHOD_PAYPAL,
        )

        if 'option_name1' in form and 'option_name2' in form:
            if (form['option_name1'] == 'anonymous' and form['option_selection1'] == 'yes') or \
                    (form['option_name2'] == 'anonymous' and form['option_selection2'] == 'yes') or \
                            form['option_name2'] == 'yes':
                new_donation.anonymous = True
            if (form['option_name1'] == 'contact' and form['option_selection1'] == 'yes') or \
                    (form['option_name2'] == 'contact' and form['option_selection2'] == 'yes') or \
                            form['option_name2'] == 'yes':
                new_donation.can_contact = True

        db.session.add(new_donation)
        db.session.commit()
        logging.info('PayPal: Payment added. ID: %s.', new_donation.id)

        send_receipt(
            new_donation.email,
            new_donation.payment_date,
            new_donation.amount,
            '%s %s' % (new_donation.first_name, new_donation.last_name),
            new_donation.editor_name,
        )

    @classmethod
    def verify_and_log_wepay_checkout(cls, checkout_id, editor, anonymous, can_contact):
        logging.debug('Processing WePay checkout...')

        # Looking up updated information about the object
        wepay = WePay(production=current_app.config['PAYMENT_PRODUCTION'],
                      access_token=current_app.config['WEPAY_ACCESS_TOKEN'])
        details = wepay.call('/checkout', {'checkout_id': checkout_id})

        if 'error' in details:
            logging.warning('WePay: Error: %s', details['error_description'])
            return False

        if 'gross' not in details:
            logging.warning('WePay: The total dollar amount paid is missing')
            return False

        if details['gross'] < 0.50:
            # Tiny donation
            logging.info('WePay: Tiny donation ($%s).', details['gross'])
            return True

        if details['state'] in ['settled', 'captured']:
            # Payment has been received

            # Checking that txn_id has not been previously processed
            if cls.get_by_transaction_id(details['checkout_id']) is not None:
                logging.info('WePay: Transaction ID %s has been used before.', details['checkout_id'])
                return

            new_donation = cls(
                first_name=details['payer_name'],
                last_name='',
                email=details['payer_email'],
                editor_name=editor,
                can_contact=can_contact,
                anonymous=anonymous,
                amount=details['gross'] - details['fee'],
                fee=details['fee'],
                transaction_id=checkout_id,
                payment_method=PAYMENT_METHOD_WEPAY,
            )

            if 'shipping_address' in details:
                address = details['shipping_address']
                new_donation.address_street = "%s\n%s" % (address['address1'], address['address2'])
                new_donation.address_city = address['city']
                if 'state' in address:  # US address
                    new_donation.address_state = address['state']
                else:
                    new_donation.address_state = address['region']
                if 'zip' in address:  # US address
                    new_donation.address_postcode = address['zip']
                else:
                    new_donation.address_postcode = address['postcode']

            db.session.add(new_donation)
            db.session.commit()
            logging.info('WePay: Payment added. ID: %s.', new_donation.id)

            send_receipt(
                new_donation.email,
                new_donation.payment_date,
                new_donation.amount,
                '%s %s' % (new_donation.first_name, new_donation.last_name),
                new_donation.editor_name,
            )

        elif details['state'] in ['authorized', 'reserved']:
            # Payment is pending
            logging.info('WePay: Payment is pending. State: "%s".', details['state'])
            pass

        elif details['state'] in ['expired', 'cancelled', 'failed', 'refunded', 'chargeback']:
            # Payment has failed
            logging.warning('WePay: Payment has failed. State: "%s".', details['state'])
            pass

        else:
            # Unknown status
            logging.warning('WePay: Unknown status.')
            return False

        return True

    @classmethod
    def log_stripe_charge(cls, charge):
        """Log successful Stripe charge.

        Args:
            charge: The charge object from Stripe. More information about it is
                available at https://stripe.com/docs/api/python#charge_object.
        """
        logging.debug('Processing Stripe charge...')

        bt = stripe.BalanceTransaction.retrieve(charge.balance_transaction)

        new_donation = cls(
            first_name=charge.source.name,
            last_name='',
            amount=bt.net / 100,  # cents should be converted
            fee=bt.fee / 100,  # cents should be converted
            transaction_id=charge.id,
            payment_method=PAYMENT_METHOD_STRIPE,

            address_street=charge.source.address_line1,
            address_city=charge.source.address_city,
            address_state=charge.source.address_state,
            address_postcode=charge.source.address_zip,
            address_country=charge.source.address_country,

            email=charge.metadata.email,
            can_contact=charge.metadata.can_contact == u'True',
            anonymous=charge.metadata.anonymous == u'True',
        )

        if 'editor' in charge.metadata:
            new_donation.editor_name = charge.metadata.editor

        db.session.add(new_donation)
        db.session.commit()
        logging.info('Stripe: Payment added. ID: %s.', new_donation.id)

        send_receipt(
            new_donation.email,
            new_donation.payment_date,
            new_donation.amount,
            new_donation.first_name,  # Last name is not used with Stripe
            new_donation.editor_name,
        )


class DonationAdminView(AdminModelView):
    column_labels = dict(
        id='ID',
        editor_name='MusicBrainz username',
        address_street='Street',
        address_city='City',
        address_state='State',
        address_postcode='Postal code',
        address_country='Country',
    )
    column_descriptions = dict(
        can_contact='This donor may be contacted',
        anonymous='This donor wishes to remain anonymous',
        amount='USD',
        fee='USD',
    )
    column_list = (
        'id',
        'email',
        'first_name',
        'last_name',
        'editor_name'
        'amount',
        'fee',
    )
    form_columns = (
        'first_name',
        'last_name',
        'email',
        'address_street',
        'address_city',
        'address_state',
        'address_postcode',
        'address_country',
        'editor_name',
        'amount',
        'fee',
        'memo',
        'can_contact',
        'anonymous',
    )

    def __init__(self, session, **kwargs):
        super(DonationAdminView, self).__init__(Donation, session, name='Donations', **kwargs)

    def after_model_change(self, form, new_donation, is_created):
        if is_created:
            send_receipt(
                new_donation.email,
                new_donation.payment_date,
                new_donation.amount,
                '%s %s' % (new_donation.first_name, new_donation.last_name),
                new_donation.editor_name,
            )
