from cloudbot import hook
from cloudbot.event import EventType
import pika
from munch import Munch
import datetime
import json
import re
import logging

MQ_HOST = "rabbitmq.home.arpa"
MQ_USER = "user"
MQ_PASS = "password"
MQ_SSL = False
SEND_QUEUE = "IRCToDiscord"
RECV_QUEUE = "DiscordToIRC"
IRC_CONNECTION = "freenode"
CHANNEL = "#effprime-bot"
QUEUE_CHECK_SECONDS = 2
STALE_PERIOD_SECONDS = 600

IRC_BOLD = ""
IRC_ITALICS = ""

log = logging.getLogger("relay_plugin")

def serialize(type_, event):
    data = Munch()

    # event data
    data.event = Munch()
    data.event.type = type_
    data.event.time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    data.event.content = getattr(event, "content", None) or getattr(event, "content_raw", None)
    data.event.command = getattr(event, "discord_command", None)
    data.event.target = event.target

    # author data
    data.author = Munch()
    data.author.nickname = event.nick
    data.author.username = event.user
    data.author.mask = event.mask

    # server data
    data.server = Munch()
    data.server.host = event.host

    # channel data
    data.channel = Munch()
    data.channel.name = event.chan

    # permissions data
    data.permissions = Munch()
    data.permissions.op = event.has_permission("op") or event.has_permission("chanop")

    as_json = data.toJSON()
    log.warning(f"Serialized data: {as_json}")
    return as_json

def deserialize(body):    
    try:
        deserialized = Munch.fromJSON(body)
    except Exception:
        log.warning(f"Unable to Munch-deserialize incoming data")
        return

    time = deserialized.event.time
    if not time:
        log.warning(f"Unable to retrieve time object from incoming data")
        return
    if time_stale(time):
        log.warning(
            f"Incoming data failed stale check ({STALE_PERIOD_SECONDS} seconds)"
        )
        return

    log.warning(f"Deserialized data: {body})")
    return deserialized

def time_stale(time):
    time = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if (now - time).total_seconds() > STALE_PERIOD_SECONDS:
        return True
    return False

def get_mq_connection():
    # blocking but what ya gonna do?
    try:
        parameters = pika.ConnectionParameters(
            MQ_HOST,
            5671 if MQ_SSL else 5672,
            "/",
            pika.PlainCredentials(MQ_USER, MQ_PASS)
        )
        return pika.BlockingConnection(parameters)
    except Exception as e:
        e = e or "No route to host" # dumb correction to a blank error
        log.warning(f"Unable to connect to RabbitMQ: {e}")

def publish(body):
    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting publish: {body}")
            return
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        mq_channel.basic_publish(
            exchange="", routing_key=SEND_QUEUE, body=body
        )
        mq_connection.close()
    except Exception as e:
        log.warning(f"Unable to publish body to queue {SEND_QUEUE}: {e}")

def consume():
    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting consume")
            return
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        method, _, body = mq_channel.basic_get(queue=RECV_QUEUE)
        if method:
            mq_channel.basic_ack(method.delivery_tag)
        mq_connection.close()
        return body
    except Exception as e:
        log.warning(f"Unable to consume from queue {RECV_QUEUE}: {e}")

def format_message(data):
    if data.event.type == "message":
        return _format_chat_message(data)

def _get_permissions_label(permissions):
    if permissions.admin:
        return "@+"
    elif permissions.ban or permissions.kick:
        return "@"
    else:
        return ""

def _format_chat_message(data):
    attachment_urls = ", ".join(data.event.attachments) if data.event.attachments else ""
    attachment_urls = f"... {attachment_urls}" if attachment_urls else ""
    return f"{IRC_BOLD}[Discord]{IRC_BOLD} {IRC_ITALICS}{_get_permissions_label(data.permissions)}{data.author.username}{IRC_ITALICS}: {data.event.content} {attachment_urls}"

@hook.regex(re.compile(r'[\s\S]+'))
def irc_message_relay(event, match):
    if event.chan != CHANNEL:
        return

    publish(serialize("message", event))

@hook.event([
    EventType.join, 
    EventType.part, 
    EventType.kick, 
    EventType.action, 
    EventType.other
])
def irc_event_relay(event):
    if event.chan != CHANNEL:
        return

    lookup = {
        EventType.join: "join",
        EventType.part: "part",
        EventType.kick: "kick",
        # EventType.action: "action", # on TODO: no clue how to deal with this right now
        EventType.other: "other"
    }
    publish(serialize(lookup[event.type], event))

@hook.command("discord", permissions=["op", "chanop"])
def discord_command(text, nick, db, conn, mask, event):
    if event.chan != CHANNEL:
        log.warning(f"Discord command issued outside of channel {CHANNEL}")
        return f"{nick}: that command can only be used from the Discord relay channel ({CHANNEL})"

    args = text.split(" ")
    if len(args) > 0:
        command = args[0]
        if command in ["kick", "ban", "unban"] and len(args) > 1:
            target = args[1]
            event.discord_command = command
            event.content = target
            publish(serialize("command", event))

@hook.periodic(QUEUE_CHECK_SECONDS)
def discord_receiver(bot):
    response = consume()
    if response:
        handle_event(bot, response)

def handle_event(bot, response):
    data = deserialize(response)
    if not data:
        log.warning("Unable to deserialize data! Aborting!")
        return

    event_type = data.event.type

    # handle message event
    if event_type in ["message"]:
        message = format_message(data)
        if message:
            try:
                bot.connections.get(IRC_CONNECTION).message(CHANNEL, message)
            except Exception as e:
                log.warning(f"Unable to send message to {CHANNEL}: {e}")
        else:
            log.warning(f"Unable to format message for event: {response}")

    # handle command event
    elif event_type == "command":
        process_command(bot, data)

    else:
        log.warning(f"Unable to handle event: {response}")

def process_command(bot, data):
    if not getattr(data.permissions, data.event.command) == True:
        log.warning(f"Blocking incoming {data.event.command} request")
        return

    action = ""
    if data.event.command == "kick":
        action = f"KICK {CHANNEL} {data.event.content} :kicked by {data.author.username} from Discord"

    elif data.event.command == "ban":
        action = f"MODE {CHANNEL} +b {data.event.content}!*@*"

    elif data.event.command == "unban":
        action = f"MODE {CHANNEL} -b {data.event.content}!*@*"

    if action:
        try:
            bot.connections.get(IRC_CONNECTION).send(action)
        except Exception as e:
            log.warning(f"Unable to send command to {CHANNEL}: {e}")   
    else:
        log.warning(f"Received unroutable command: {data.event.command}")

# TODO:
# send more information for mode changes (showing as `other` events)