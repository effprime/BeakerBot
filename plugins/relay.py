from cloudbot import hook
import asyncio
import pika
import datetime
import json
import re

MQ_HOST = ""
SEND_QUEUE = ""
RECV_QUEUE = ""
IRC_CONNECTION = ""
CHANNEL = ""

def serialize(event, message):
    data = {}
    data["time"] = datetime.datetime.now()
    data["author_nick"] = event.nick
    data["channel_name"] = event.chan
    data["message"] = message
    return json.dumps(data, default=str)

def deserialize(body):
    try:
        deserialized = json.loads(body)
    except Exception:
        return None

    time = deserialized.get("time")
    author_name = deserialized.get("author_name")
    message = deserialized.get("message")
    if author_name and message and time:
        time = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f")
        time = time.strftime('%H:%M:%S')

        return f"[DISCORD@{time}] {author_name}: {message}"

@hook.regex(re.compile(r'[\s\S]+'))
async def irc_message_relay(event, match):
    if event.chan != CHANNEL:
        return

    message = match.group()

    mq_connection = pika.BlockingConnection(
            pika.URLParameters(MQ_HOST)
        )
    mq_channel = mq_connection.channel()
    mq_channel.queue_declare(queue=SEND_QUEUE, durable=True)
    mq_channel.basic_publish(
        exchange="", routing_key=SEND_QUEUE, body=serialize(event, message)
    )

@asyncio.coroutine
@hook.periodic(1)
async def discord_message_receiver(bot):
    irc_conn = bot.connections.get(IRC_CONNECTION)
    mq_connection = pika.BlockingConnection(
            pika.URLParameters(MQ_HOST)
        )
    mq_channel = mq_connection.channel()
    mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
    if irc_conn:
        try:
            method, header, body = mq_channel.basic_get(queue=RECV_QUEUE)
            if method:
                mq_channel.basic_ack(method.delivery_tag)
                message = deserialize(body)
                if message:
                    irc_conn.message(CHANNEL, message)
        except Exception:
            pass
