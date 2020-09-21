from cloudbot import hook
from cloudbot.event import Event, EventType
import pika
from munch import Munch
import datetime
import json
import re
import logging
import uuid

from plugins.core.chan_track import get_users

MQ_HOST = "rabbitmq.home.arpa"
MQ_VHOST = "/"
MQ_USER = "user"
MQ_PASS = "password"
MQ_PORT = 5672
MQ_SSL = False
SEND_QUEUE = "IRCToDiscord"
RECV_QUEUE = "DiscordToIRC"
IRC_CONNECTION = "freenode"
CHANNEL_MAP = {
    668165200901439521: "#effprime-bot",
    674778646472556605: "#effprime-butt"
}
CHANNELS = list(CHANNEL_MAP.values())
QUEUE_CHECK_SECONDS = 1
QUEUE_SEND_SECONDS = 1
STALE_PERIOD_SECONDS = 600
COMMANDS_ALLOWED = True
RESPONSE_LIMIT = 3
SEND_LIMIT = 3
SEND_JOIN_PART_EVENTS = False

IRC_BOLD = ""
IRC_ITALICS = ""

log = logging.getLogger("relay_plugin")

# limits connections we have to make per hook
send_buffer = []

def get_permission(event):
    try:
        users = get_users(event.conn)
        user = users.get(event.nick)
        if user:
            member = user.channels.get(event.chan) if user else None
            if member:
                modes = []
                for entry in member.status:
                    modes.append(entry.mode)
                return modes
    except Exception as e:
        log.warning(f"Unable to get permissiaons for {event.nick}: {e}")
    return []

def serialize(type_, event):
    data = Munch()

    # event data
    data.event = Munch()
    data.event.type = type_
    data.event.uuid = str(uuid.uuid4())
    data.event.time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    data.event.content = getattr(event, "content", None) or getattr(event, "content_raw", None)
    data.event.command = getattr(event, "discord_command", None)
    data.event.target = event.target
    data.event.irc_raw = event.irc_raw
    data.event.irc_prefix = event.irc_prefix
    data.event.irc_command = event.irc_command
    data.event.irc_paramlist = event.irc_paramlist
    data.event.irc_ctcp_text = event.irc_ctcp_text

    # author data
    data.author = Munch()
    data.author.nickname = event.nick
    data.author.username = event.user
    data.author.mask = event.mask
    data.author.host = event.host
    data.author.permissions = get_permission(event)

    # server data
    data.server = Munch()
    data.server.name = event.conn.name
    data.server.nick = event.conn.nick
    data.server.channels = event.conn.channels

    # channel data
    data.channel = Munch()
    data.channel.name = event.chan

    as_json = data.toJSON()
    log.warning(f"Serialized data: {as_json}")
    return as_json

def deserialize(body):    
    try:
        deserialized = Munch.fromJSON(body)
    except Exception as e:
        log.warning(f"Unable to Munch-deserialize incoming data: {e}")
        log.warning(f"Full body: {body}")
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
            MQ_PORT,
            MQ_VHOST,
            pika.PlainCredentials(MQ_USER, MQ_PASS)
        )
        return pika.BlockingConnection(parameters)
    except Exception as e:
        e = e or "No route to host" # dumb correction to a blank error
        log.warning(f"Unable to connect to RabbitMQ: {e}")

def publish(bodies):
    mq_connection = None

    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting publish: {bodies}")
            return
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        for body in bodies:
            mq_channel.basic_publish(
                exchange="", routing_key=SEND_QUEUE, body=body
            )

    except Exception as e:
        log.warning(f"Unable to publish body to queue {SEND_QUEUE}: {e}")

    if mq_connection:
        mq_connection.close()

def consume():
    mq_connection = None
    bodies = []

    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting consume")
            return bodies
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        
        checks = 0
        while checks < RESPONSE_LIMIT:
            body = get_ack(mq_channel)
            checks += 1
            if not body:
                break
            bodies.append(body)

    except Exception as e:
        log.warning(f"Unable to consume from queue {RECV_QUEUE}: {e}")

    if mq_connection:
        mq_connection.close()

    return bodies

def get_ack(channel):
    method, _, body = channel.basic_get(queue=RECV_QUEUE)
    if method:
        channel.basic_ack(method.delivery_tag)
        return body

def format_message(data):
    if data.event.type in ["message", "factoid"]:
        return _format_chat_message(data)

def _get_permissions_label(permissions):
    if permissions.admin:
        return "**"
    elif permissions.ban or permissions.kick:
        return "*"
    else:
        return ""

def _format_chat_message(data):
    attachment_urls = ", ".join(data.event.attachments) if data.event.attachments else ""
    attachment_urls = f" {attachment_urls}" if attachment_urls else ""
    return f"{IRC_BOLD}[D]{IRC_BOLD} <{_get_permissions_label(data.author.permissions)}{data.author.username}> {data.event.content} {attachment_urls}"

@hook.regex(re.compile(r'[\s\S]+'))
def irc_message_relay(event, match):
    if event.chan not in CHANNELS:
        log.warning(f"Ignoring channel {event.chan} because not in {CHANNELS}")
        return
    send_buffer.append(serialize("message", event))

@hook.periodic(QUEUE_SEND_SECONDS)
def irc_publish(bot):
    global send_buffer
    bodies = [
        body for idx, body in enumerate(send_buffer) if idx+1 <= SEND_LIMIT
    ]
    bodies = append_factoids(bot.connections.get(IRC_CONNECTION), bodies)
    if bodies:
        publish(bodies)
        send_buffer = send_buffer[len(bodies):]

def append_factoids(conn, bodies):
    if conn:
        MAX_FACTOIDS = 5
        memory = conn.memory.get("factoids")
        if memory:
            limit = MAX_FACTOIDS if MAX_FACTOIDS <= len(memory) else len(memory)
            for i in range(0, limit):
                bodies.append(serialize("factoid", memory[i]))
            conn.memory["factoids"] = conn.memory["factoids"][limit:]
    return bodies

@hook.event([
    EventType.join, 
    EventType.part,
    EventType.kick, 
    EventType.other,
    EventType.notice
])
def irc_event_relay(event):
    if event.chan not in CHANNELS:
        return

    lookup = {
        EventType.join: "join",
        EventType.part: "part",
        EventType.kick: "kick",
        # EventType.action: "action", # on TODO: no clue how to deal with this right now
        EventType.other: "other",
        EventType.notice: "notice"
    }

    if lookup[event.type] in ["join", "part"] and not SEND_JOIN_PART_EVENTS:
        return

    send_buffer.append(serialize(lookup[event.type], event))

@hook.irc_raw("QUIT")
def irc_quit_event_relay(event):
    send_buffer.append(serialize("quit", event))

@hook.command("discord", permissions=["op", "chanop"])
def discord_command(text, nick, db, conn, mask, event):
    if not COMMANDS_ALLOWED:
        return f"{nick}: relay cross-chat commands have been disabled on this bot" 

    if event.chan not in CHANNELS:
        log.warning("Discord command issued outside of channel")
        return f"{nick}: that command can only be used from the Discord relay channels"

    args = text.split(" ")
    if len(args) > 0:
        command = args[0]
        if len(args) > 1:
            target = " ".join(args[1:])
            event.discord_command = command
            event.content = target
            send_buffer.append(serialize("command", event))

@hook.periodic(QUEUE_CHECK_SECONDS)
async def discord_receiver(bot):
    responses = consume()
    for response in responses:
        await handle_event(bot, response)

def _get_channel(data):
    for channel_name in CHANNELS:
        if channel_name == CHANNEL_MAP.get(data.channel.id):
            return channel_name

async def handle_event(bot, response):
    data = deserialize(response)
    if not data:
        log.warning("Unable to deserialize data! Aborting!")
        return

    event_type = data.event.type
    # handle message event
    if event_type in ["message"]:
        message = format_message(data)
        if message:
            channel = _get_channel(data)
            if not channel:
                log.warning("Unable to find channel to send message")
                return
            try:
                bot.connections.get(IRC_CONNECTION).message(channel, message)
            except Exception as e:
                log.warning(f"Unable to send message to {channel}: {e}")
        else:
            log.warning(f"Unable to format message for event: {response}")

    # handle command event
    elif event_type == "command":
        process_command(bot, data)

    elif event_type == "factoid":
        await process_factoid_request(bot, data)

    else:
        log.warning(f"Unable to handle event: {response}")

def process_command(bot, data):
    if not COMMANDS_ALLOWED:
        log.warning(f"Blocking incoming {data.event.command} request due to disabled config")
        return

    if data.event.command in ["kick", "ban", "unban"]:
        _process_user_command(bot, data)
    elif data.event.command == "whois":
        _process_whois_command(bot, data)
    else:
        log.warning(f"Received unroutable command: {data.event.command}")

def _process_user_command(bot, data):
    if not getattr(data.author.permissions, data.event.command) == True and not getattr(data.author.permissions, "admin"):
        log.warning(f"Blocking incoming {data.event.command} request due to permissions")
        return

    channel = _get_channel(data)
    if not channel:
        log.warning("Unable to find channel to send message")
        return

    bot.connections.get(IRC_CONNECTION).message(
        channel,
        f"Executing IRC {data.event.command} command from {data.author.username} on target {data.event.content}"
    )

    action = ""
    if data.event.command == "kick":
        action = f"KICK {channel} {data.event.content} :kicked by {data.author.username} from Discord"

    elif data.event.command in  ["ban", "unban"]:
        mode = "+" if data.event.command == "ban" else "-"
        if data.event.content.count(".") == 3:
            # very likely to be an IP
            # cant wait to see the edge case
            action = f"MODE {channel} {mode}b *!*@*{data.event.content}"
        else:
            action = f"MODE {channel} {mode}b {data.event.content}!*@*"

    elif data.event.command == "unban":
        action = f"MODE {channel} -b {data.event.content}!*@*"

    if action:
        try:
            bot.connections.get(IRC_CONNECTION).send(action)
        except Exception as e:
            log.warning(f"Unable to send command to {channel}: {e}")

def _process_whois_command(bot, data):
    connection = bot.connections.get(IRC_CONNECTION)
    
    # this seems to help
    connection.cmd("WHOIS", data.event.content)

    users = connection.memory.get("users", {})
    user = users.getuser(data.event.content)

    request = Munch()
    request.author = data.author.id
    request.event = data.event.id

    payload = Munch()
    payload.nick = user.mask.nick
    payload.user = user.mask.user
    payload.host = user.mask.host
    payload.realname = user.realname
    payload.server = user.server
    payload.away = user.is_away
    payload.away_message = user.away_message
    payload.channels = list(user.channels.keys())

    response = Munch()
    response.request = request
    response.type = "whois"
    response.payload = payload

    event = Event(bot=bot, conn=connection, channel=_get_channel(data))
    event.content = response

    send_buffer.append((serialize("response", event)))

async def process_factoid_request(bot, data):
    channel = _get_channel(data)
    if not channel:
        log.warning("Unable to find channel to send message")
        return

    bot.connections.get(IRC_CONNECTION).message(channel, format_message(data))
    event = Event(
        bot=bot, conn=bot.connections.get(IRC_CONNECTION), event_type=EventType.message, content_raw=data.event.content, content=data.event.content,
        target="factoid_request", channel=channel, nick='janedoe', user="~janedoe", host="unaffiliated/janedoe", mask="janedoe!~janedoe@unaffiliated/janedoe", irc_raw="",
        irc_prefix="janedoe!~janedoe@unaffiliated/janedoe", irc_command="PRIVMSG", irc_paramlist=[channel, data.event.content], irc_ctcp_text=None
    )
    await bot.process(event)
