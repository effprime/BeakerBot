import asyncio
from collections import defaultdict, deque
from datetime import datetime

import cloudbot
from cloudbot import hook

times = defaultdict(lambda: defaultdict(lambda: defaultdict()))
messages = defaultdict(lambda: defaultdict(lambda: defaultdict()))
strikes = defaultdict(lambda: defaultdict(lambda: defaultdict()))

chars = '~`!@#$%^&*()_+=-|}{[]\\\';:"/.,<>?``¡™£¢∞§¶•ªº–≠«‘“…æ÷≥≤¿ÚÆ”’»±—‚·°‡ﬂﬁ›'

@asyncio.coroutine
@hook.irc_raw("PRIVMSG")
def parse_privmsg(conn, nick, user, host, chan, irc_raw, admin_log, event):
    if not "o" in get_permission(event):
        message = ' '.join(''.join(irc_raw.split(':', 2)).split()[3:])
        message = message.translate(message.maketrans('','', chars))
        if len(message) > 0:
            if ord(message[0]) == 1:
                if message[1:].startswith('ACTION'):
                    message = message[8:]
            config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
            if config:
                add_msg(conn, chan, nick, message)
                check_flood(conn, chan, nick, user, host, config, admin_log)
                check_repeat(conn, chan, nick, user, host, config, admin_log)
                check_caps(conn, chan, nick, user, host, message, config, admin_log)
                
def get_permission(event):
    try:
        users = get_users(event.conn)
        user = users.get(event.nick)
        if user:
            member = user.channels.get(CHANNEL) if user else None
            if member:
                modes = []
                for entry in member.status:
                    modes.append(entry.mode)
                return modes
    except Exception:
        pass
    return []

@asyncio.coroutine
@hook.irc_raw("JOIN")
def on_join(conn, chan, nick):
    config = None
    config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
    if config:
        lines = config['lines']; repeat = config['repeat']
        if nick == conn.nick:
            setup_user(conn, chan, nick, lines, repeat)

@asyncio.coroutine
@hook.irc_raw("PART")
def on_part(conn, chan, nick):
    remove_user(conn, chan, nick)

@asyncio.coroutine
@hook.irc_raw("KICK")
def on_kick(conn, nick, chan, target, irc_raw):
    try:
        ltarget = target.lower(); lchan = target.lower()
        if strikes[conn.name][lchan][ltarget] == 3:
            remove_user(conn, chan, target)
    except KeyError:
        pass
    op = nick
    reason = irc_raw.split(':')[2]

    opnotices = None
    try:
        opnotices = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opnotices']
    except:
        pass
    if opnotices:
        if nick != conn.nick:
            try:
                opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
                out = "PRIVMSG {} :\x02CHANPROTECT\x02: {} kicked {} in {}. Reason: {}".format(opchan, op, target, chan, reason)
                conn.send(out)
            except KeyError:
                pass

@asyncio.coroutine
@hook.irc_raw("QUIT")
def on_quit(conn, nick):
    for chan in times[conn.name]:
        remove_user(conn, chan, nick)

@asyncio.coroutine
@hook.irc_raw("MODE")
def on_mode(conn, nick, chan, irc_raw):
    opnotices = None
    try:
        opnotices = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opnotices']
    except KeyError:
        pass

    if opnotices:
        mode = irc_raw.split()[3]
        if mode == "+b" or mode == "+q" or mode == "-b" or mode == "-q":
            try: 
                target = irc_raw.split()[4]
                if "b" in mode:
                    type = "ban"
                elif "q" in mode:
                    type = "quiet"

                if "+" in mode:
                    action = "added"
                elif "-" in mode:
                    action = "removed"

                opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
                out = "PRIVMSG {} :\x02CHANPROTECT\x02: {} {} {} on {} in {}.".format(opchan, nick, action, type, target, chan)
                conn.send(out)

            except KeyError:
                pass

def check_flood(conn, chan, nick, ident, host, config, admin_log):
    opchan = None
    try:
        opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
    except:
        pass

    lines = config['lines']; secs = config['secs']
    lnick = nick.lower(); lchan = chan.lower()
    user_times = times[conn.name][lchan][lnick]
    if len(user_times) == lines:
        dt = user_times[lines - 1] - user_times[0]
        if dt.total_seconds() < secs:
            add_strike(conn, chan, nick)
            count = strikes[conn.name][lchan][lnick]
            action(conn, opchan, nick, ident, host, chan, count, admin_log, lines=lines, secs=secs)

def check_repeat(conn, chan, nick, ident, host, config, admin_log):
    opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
    max = config['repeat']
    lnick = nick.lower(); lchan = chan.lower()
    user_messages = messages[conn.name][lchan][lnick]
    if len(user_messages) == max:
        if len(set(user_messages)) == 1:
            add_strike(conn, chan, nick)
            count = strikes[conn.name][lchan][lnick]
            action(conn, opchan, nick, ident, host, chan, count, admin_log, max=max)

def check_caps(conn, chan, nick, ident, host, message, config, admin_log):
    config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
    upper_count = 0
    for letter in message:
        if letter.isupper():
            upper_count += 1
    percent = upper_count / len(message)
    if percent >= config['capratio'] and len(message) >= config['caplength']:
        add_strike(conn, chan, nick)
        lnick = nick.lower(); lchan = chan.lower()
        count = strikes[conn.name][lchan][lnick]
        action(conn, config['opchan'], nick, ident, host, chan, count, admin_log, percent=percent)

@hook.command("warn", permissions=["op"])
def warn(conn, nick, user, host, text, notice, admin_log):
    """warn <nick> <#channel> <reason>"""
    try:
        text = text.split()
        op = nick
        chan = text[1]
        nick = text[0]
        lnick = nick.lower(); lchan = chan.lower()
        reason = ' '.join(text[2:])
        opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
        add_strike(conn, chan, nick)
        count = strikes[conn.name][lchan][lnick]
        action(conn, opchan, nick, user, host, chan, count, admin_log, manual=True, reason=reason, op=op, notice=notice)
    except:
        return notice(".warn <nick> <#channel> <reason>")

def action(conn, opchan, nick, ident, host, chan, count, admin_log, **kwargs):
    url = conn.config.get("chanprotect", {}).get(chan.lower(), {})['rules']
    if count == 1:
        action = "WARNING"
    elif count == 2:
        action = "KICK"
    elif count == 10:
        action = "BAN"

    if kwargs.get('lines', None) and kwargs.get('secs', None):
        reason = "Flooding in {} (Exceeded {} lines in {} seconds).".format(chan, kwargs['lines'], kwargs['secs'])
    elif kwargs.get('max', None):
        reason = "Spamming in {} (Repeated same message {} times).".format(chan, kwargs['max'])
    elif kwargs.get('percent', None):
        reason = "Excess CAPS in {}.".format(chan)
    elif kwargs.get('manual', False):
        notice = kwargs['notice']; op = kwargs['op']; reason = kwargs['reason']
        notice("Warning sent to {} in {} ({} {}/3). Reason: {}".format(nick, chan, action, count, reason))
        reason = "Manual warning in {}. Reason: {}".format(chan, reason)

    if count == 1:
        out = "NOTICE {} :\x02{}\x02 ({}/3): {}".format(nick, action, count, reason)
    elif count == 2 or count == 3:
        out = "KICK {} {} :{} ({}/3): {}".format(chan, nick, action, count, reason)

    if 1 <= count <= 4:
        rules = 'Violation of channel rules: {}'.format(url)
        out = "{} ({})".format(out, rules)
        conn.send(out)

        if kwargs.get('manual', False):
            user = nick
            if count == 10:
                out = "MODE {} +b {}!*@*".format(chan, nick)
                conn.send(out)
        else:
            user = "{}!{}@{}".format(nick, ident, host)
            if count == 10:
                out = "MODE {} +b *!*@{}".format(chan, host)
                conn.send(out)

        text = "\x02CHANPROTECT\x02: User {}!{}@{} triggered {} ({}/3): {}".format(nick, ident, host, action, count, reason)
        if kwargs.get('manual', False):
                text = "{} (op: {})".format(text, op)

        if opchan:
            conn.send("PRIVMSG {} :{}".format(opchan, text))

        admin_log(text)

def add_msg(conn, chan, nick, message):
    lnick = nick.lower(); lchan = chan.lower()
    try:
        times[conn.name][lchan][lnick].append(datetime.now())
        messages[conn.name][lchan][lnick].append(message)
    except:
        config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
        if config:
            lines = config['lines']; repeat = config['repeat']
            setup_user(conn, chan, nick, lines, repeat)
            times[conn.name][lchan][lnick].append(datetime.now())
            messages[conn.name][lchan][lnick].append(message)

def add_strike(conn, chan, nick):
    lnick = nick.lower(); lchan = chan.lower()
    try:
        strikes[conn.name][lchan][lnick] += 1
    except:
        config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
        if config:
            lines = config['lines']; repeat = config['repeat']
            setup_user(conn, chan, nick, lines, repeat)
            strikes[conn.name][lchan][lnick] += 1

def setup_user(conn, chan, nick, lines, repeat):
    lnick = nick.lower(); lchan = chan.lower()
    times[conn.name][lchan][lnick] = deque('', maxlen=lines)
    messages[conn.name][lchan][lnick] = deque('', maxlen=repeat)
    strikes[conn.name][lchan][lnick] = 0

@hook.command("strikes", permissions=["op"])
def getstrikes(conn, text, notice, admin_log):
    try:
        text = text.split()
        nick = text[0]
        chan = text[1]
        lnick = nick.lower(); lchan = chan.lower()

        user_strikes = strikes[conn.name][lchan][lnick]
        user_times = ', '.join([t.strftime("%Y-%m-%d %H:%M:%S.%f") for t in times[conn.name][lchan][lnick]])
        user_messages = ' /// '.join([m for m in messages[conn.name][lchan][lnick]])
        notice("Strikes for {} in {}: {}".format(nick, chan, user_strikes))
        notice("Times for {} in {}: {}".format(nick, chan, user_times))
        notice("Messages for {} in {}: {}".format(nick, chan, user_messages))
    except KeyError:
        notice("Information for user {} in {} not found.".format(nick, chan))

@hook.command("clearstrikes", permissions=["op"])
def clearstrikes(conn, nick, text, notice, admin_log):
    """clearstrikes <nick> <#channel>"""
    try:
        text = text.split()
        op = nick
        chan = text[1]
        nick = text[0]
        opchan = conn.config.get("chanprotect", {}).get(chan.lower(), {})['opchan']
        remove_user(conn, chan, nick)
        notice("Strikes for {} cleared in {}.".format(nick, chan))
        out = "NOTICE {} :Your strikes in {} have been cleared by {}.".format(nick, chan, op)
        conn.send(out)
        text = "\x02CHANPROTECT\x02: {} cleared strikes for {} in {}.".format(op, nick, chan)
        conn.send("PRIVMSG {} :{}".format(opchan, text))
        admin_log(text)
    except:
        return notice(".clearstrikes <nick> <#channel>")

def remove_user(conn, chan, nick):
    lnick = nick.lower(); lchan = chan.lower()
    config = conn.config.get("chanprotect", {}).get(chan.lower(), {})
    if config:
        if nick == conn.nick:
            try:
                del times[conn.name][lchan]
                del messages[conn.name][lchan]
                del strikes[conn.name][lchan]
            except:
                pass
        else:
            if nick in times[conn.name][lchan]:
                try:
                    del times[conn.name][lchan][lnick]
                    del messages[conn.name][lchan][lnick]
                    del strikes[conn.name][lchan][lnick]
                except:
                    pass
