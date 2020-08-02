from cloudbot import hook
import asyncio
import pika
import datetime
import json
import re

MQ_HOST = "rabbitmq.home.arpa"
MQ_USER = "user"
MQ_PASS = "password"
MQ_SSL = False
SEND_QUEUE = "IRCToDiscord"
RECV_QUEUE = "DiscordToIRC"
IRC_CONNECTION = "freenode"
CHANNEL = "#effprime-bot"

def serialize(event, message):
    data = {}
    data["time"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    data["author_nick"] = event.nick
    data["channel_name"] = event.chan
    data["message"] = message
    return json.dumps(data)

def deserialize(body):
    try:
        deserialized = json.loads(body)
    except Exception:
        return None

    time = deserialized.get("time")
    if not time:
        return
        
    time = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if (now - time).total_seconds() > 600:
        return

    author_name = deserialized.get("author_name")
    message = deserialized.get("message")
    if author_name and message:
        return f"[D] {author_name}: {message}"

@hook.regex(re.compile(r'[\s\S]+'))
async def irc_message_relay(event, match):
    if event.chan != CHANNEL:
        return

    message = match.group()
    if message.startswith(event.conn.config.get('command_prefix')):
        return

    parameters = pika.ConnectionParameters(
        MQ_HOST,
        5671 if MQ_SSL else 5672,
        "/",
        pika.PlainCredentials(MQ_USER, MQ_PASS)
    )
    mq_connection = pika.BlockingConnection(parameters)
    mq_channel = mq_connection.channel()
    mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
    mq_channel.basic_publish(
        exchange="", routing_key=SEND_QUEUE, body=serialize(event, message)
    )
    mq_connection.close()

@asyncio.coroutine
@hook.periodic(1)
async def discord_message_receiver(bot):
    irc_conn = bot.connections.get(IRC_CONNECTION)
    parameters = pika.ConnectionParameters(
        MQ_HOST,
        5671 if MQ_SSL else 5672,
        "/",
        pika.PlainCredentials(MQ_USER, MQ_PASS)
    )
    mq_connection = pika.BlockingConnection(parameters)
    mq_channel = mq_connection.channel()
    mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
    if irc_conn:
        try:
            method, _, body = mq_channel.basic_get(queue=RECV_QUEUE)
            if method:
                mq_channel.basic_ack(method.delivery_tag)
                message = deserialize(body)
                if message:
                    irc_conn.message(CHANNEL, message)
        except Exception as e:
            print(e)
    mq_connection.close()
