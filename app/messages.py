"""User-facing text. All in one place so it is easy to translate (Amharic / Tigrinya)."""

START = (
    "🎟 Welcome to {campaign_name}!\n\n"
    "• Ticket price: {price} ETB per ticket\n"
    "• You can buy multiple tickets in one payment: send 200 ETB for 2 tickets, 300 ETB for 3 tickets, 400 ETB for 4 tickets, etc. (up to {max_tickets} tickets per transfer)\n"
    "{campaign}\n"
    "{instructions}\n\n"
    "After paying, send the screenshot of your payment receipt, a CBE PDF receipt, or paste your CBE receipt link here. "
    "For screenshots, send the FULL image (not cropped) with the QR code visible so your ticket is verified instantly.\n\n"
    "/mytickets – see your ticket numbers"
)
CAMPAIGN_LINE = "• Campaign: {start} → {end}\n"
RECEIVED = "📥 Receipt received. Verifying your payment – this usually takes a minute."
NOT_STARTED = "The campaign has not started yet."
ENDED = "The campaign has ended. Payments are no longer accepted."
SOLD_OUT = "❌ All lottery tickets have been sold out! No further payments are being accepted."
TOO_MANY_OPEN = "You already have {n} payments being verified. Please wait for those to finish."
SEND_SCREENSHOT = "Please send a screenshot (photo), CBE PDF receipt, or paste a CBE receipt link. /start shows how to pay."
NO_TICKETS = "You don't have any tickets yet. Send a payment receipt to get one."
NO_TICKETS_WITH_BALANCE = "You don't have any tickets yet.\n💰 Wallet balance: {balance} ETB — send {remaining} ETB more to get your ticket!"
MY_TICKETS = "🎟 Your tickets ({n}):\n{numbers}"
MY_TICKETS_WITH_BALANCE = "🎟 Your tickets ({n}):\n{numbers}\n\n💰 Wallet balance: {balance} ETB (saved toward your next ticket)"

APPROVED = "✅ Payment verified!\nYour ticket number(s): {numbers}\nGood luck! ሰናይ ዕድል 🍀"
APPROVED_WITH_BALANCE = (
    "✅ Payment verified!\nYour ticket number(s): {numbers}\n"
    "💰 Remaining wallet balance: {balance} ETB (saved toward your next ticket)\n"
    "Good luck! ሰናይ ዕድል 🍀"
)
WALLET_CREDITED = (
    "💰 {added} ETB received and saved to your wallet balance (Total: {balance} ETB).\n"
    "Send {remaining} ETB more to reach {price} ETB and receive your lottery ticket!"
)
VOIDED = "⚠️ Your ticket(s) {numbers} were cancelled/voided by the organizer.\nReason: {reason}"
WINNER_DM = (
    "🎉 Congratulations! Your ticket {number} won Prize #{rank} in {campaign_name}! 🏆\n"
    "The organizers will contact you shortly."
)
UNDER_REVIEW = (
    "🕒 We couldn't verify your payment automatically, so a person is checking it. "
    "You will get a message here as soon as it's done."
)
UNREADABLE = (
    "❌ We couldn't read a transaction reference in that image. Please send the FULL, uncropped "
    "screenshot of the receipt (the one that shows the transaction ID)."
)
NOT_AN_IMAGE = "❌ That file isn't a valid image. Please send a screenshot."
ALREADY_USED = "❌ This payment/transaction was already used for tickets."
DUPLICATE_IMAGE = "❌ This screenshot was already submitted."
UNDERPAID = "❌ The amount received ({amount} ETB) is less than one ticket ({price} ETB)."
OUTSIDE_WINDOW = "❌ This payment was received outside the campaign period."
ADMIN_REJECTED = "❌ Your payment could not be accepted. Contact the organisers if you think this is a mistake."
