# Fernwood checkout and email notifications

## Payment flow

The storefront sends the basket and delivery address to
`POST /api/payment/create-order`. Python recalculates the price from the local
product catalog, creates a Razorpay order using server-side credentials, and emails
the order summary to the configured store inbox. The email contains product details,
customer contact and delivery details, order ID, and the pending amount. Checkout is
not returned to the browser unless the order email can be sent.

After Razorpay Checkout, the browser sends Razorpay's order ID, payment ID, and
signature to `POST /api/payment/verify`. The backend verifies the signature, asks
Razorpay to confirm the captured payment, updates local inventory once, and emails a
payment-confirmed notice. Razorpay also sends signed `payment.captured` and
`order.paid` events to `POST /api/payment/webhook`. Webhook signatures are checked
against the unmodified request body; repeated payment callbacks do not reduce stock
again.

## Where data is kept

- Product details and inventory are kept in `products.json`; the existing product
  array is migrated to the app's JSON object format automatically on first launch.
- Customer details and the full product/order summary are sent to the store email.
  They are not persisted in the local order-state data or a SQL database.
- `products.json` also holds pending Razorpay order IDs, amount/currency, product
  IDs/quantities, and processed order IDs with a payment-email status. This small
  local state is required to resume payment verification after a server restart and
  prevent duplicate inventory reductions. It does not contain customer addresses,
  card numbers, CVV, or payment secrets. Back up this file and do not delete its
  order state while payments are in progress.
- This app no longer connects to MySQL. Any existing MySQL database or historical
  rows are left untouched; Fernwood does not read or delete them.
- Card entry is handled by Razorpay Checkout. Fernwood does not receive or store card
  numbers or CVV.

Email is the order-notification and order-history channel, not a transactional order
database. Keep the store inbox secure, enable multi-factor authentication, and use
mailbox retention/backup appropriate for customer and business records.

## Publish inventory changes to the Vercel site

The inventory manager saves local changes immediately. The local storefront reads
those changes from the Python API; the public Vercel storefront reads only the
product-only `catalog.json` file and does not show hard-coded demo products.

After changing products or stock in the local inventory manager:

1. Refresh the local storefront. It should show the change without a Git commit.
2. Review `catalog.json`; the local server updates this file from inventory changes.
   It contains the public product list only, not pending or processed payment state.
3. Commit and push the catalog update to the GitHub branch Vercel deploys:

   ```powershell
   git add catalog.json
   git commit -m "Update published product catalog"
   git push
   ```

4. Wait for Vercel to finish deploying, then refresh the live site. The deployed
   catalog changes only when this file is committed and pushed. Do not commit
   `products.json` for catalog publishing; it also contains payment processing state.

## Configure email using Gmail

1. Secure the receiving mailbox `harish9.cz@gmail.com` with a unique password and
   multi-factor authentication. Do not use its normal Gmail password in the app.
2. For a Gmail sending account, turn on 2-Step Verification and create a Google
   **App Password**. Use that App Password as `SMTP_PASSWORD`. The sender account
   must be allowed to send mail; the order email will be delivered to the store
   inbox above.
3. Open PowerShell in the project folder and configure SMTP and Razorpay Test Mode
   credentials in the same terminal:

   ```powershell
   cd C:\Users\User\Documents\1-e-com

   $env:SMTP_HOST = 'smtp.gmail.com'
   $env:SMTP_PORT = '587'
   $env:SMTP_USERNAME = 'your-sending-gmail@gmail.com'
   $env:SMTP_PASSWORD = 'your-16-character-google-app-password'
   $env:SMTP_FROM = 'your-sending-gmail@gmail.com'
   $env:ORDER_NOTIFICATION_EMAIL = 'harish9.cz@gmail.com'

   $env:RAZORPAY_KEY_ID = 'rzp_test_...'
   $env:RAZORPAY_KEY_SECRET = '...'
   $env:RAZORPAY_WEBHOOK_SECRET = '...'

   .\.venv\Scripts\python.exe admin.py
   ```

   Keep the terminal open while running the storefront. Do not commit these values,
   include them in browser code, or send them in chat. The Razorpay key ID is public
   to Checkout; the key secret, webhook secret, and Gmail App Password are private.

For another email provider, use its official SMTP hostname, port, and TLS
requirements. Port 587 uses STARTTLS; port 465 uses implicit TLS.

## Test with Razorpay Test Mode

1. In the Razorpay Dashboard, use **Test Mode** API keys and create a webhook for
   `payment.captured` and `order.paid`. Use
   `https://<public-HTTPS-host>/api/payment/webhook` as the webhook URL. A local
   server needs a secure HTTPS tunnel for Razorpay to reach it.
2. Make sure the app is running at `http://127.0.0.1:8000/`.
3. Add an in-stock product, enter the delivery details, and start checkout using the
   test card information in Razorpay's official documentation.
4. Confirm the inbox receives an order-created email and then a payment-confirmed
   email. Check that product stock was reduced exactly once.

An email or SMTP outage can delay the notification. If the payment email fails after
Razorpay confirms payment, the order remains processed locally and repeated webhook
delivery retries the email without reducing stock again. SMTP delivery itself cannot
guarantee that the mailbox won't receive a duplicate if the process stops immediately
after sending but before recording that the notification was sent.

Use HTTPS, Razorpay Live Mode credentials, a production-ready email provider, and a
publicly reachable webhook only after the Test Mode flow is verified. Rotate any
credentials that may have been exposed.
