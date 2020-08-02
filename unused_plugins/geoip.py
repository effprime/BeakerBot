import requests
from cloudbot import hook

@hook.command("geoip", permissions=["op", "chanop"])
def geoip(text):
    
    data = requests.get("http://ipinfo.io/%s/json" % (text)).json()

    if data.get("ip") == None:
        return "Invalid IP argument!"

    loc = "Location: %s, %s %s" % (data.get("city"), data.get("region"), data.get("country"))
    org = "Organization: %s" % (data.get("org"))

    return "%s ● %s" % (loc, org)
