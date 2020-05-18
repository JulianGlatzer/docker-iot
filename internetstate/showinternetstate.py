from influxdb import InfluxDBClient
from urllib import request
import speedtest
import sys, getopt
import secrets

def convertUptimeToMin(input):
    # input format 9 jours, 19 heures, 34 minutes    
    arr = input.split(",")
    jours=0
    if(len(arr)>=3):
        jours=float(arr[-3].replace("jours", "").replace("jour", "").strip())
    heures=0
    if(len(arr)>=2):
        heures=float(arr[-2].replace("heures", "").replace("heure","").strip())
    minutes=0
    if(len(arr)>=1):
        minutes=float(arr[-1].replace("minutes", "").replace("minute","").strip())
    return jours*24.+heures+minutes/60.

def getInternetState(url=secrets.routerurl, debug=False):
    data = request.urlopen(url)
    section=""
    sections={"general":"Informations g", "telephone":"phone","DSL":"Adsl", "Wifi":"Wifi", "network":"seau :"}
    info={}

    for line in data: # files are iterable
        line=line.decode('windows-1252')
        #print(line, end='')
        for key in sections.keys():
            if sections[key] in line:
                section=key
                
        arr=[x for x in line.strip().split("  ") if x!=""]

        if section=="general" and "Version du firmware" in line:
            info["firmware"]=line.strip().split("  ")[-1]
        if section=="general" and "Temps depuis la mise en route" in line:
            info["uptime"]=convertUptimeToMin(line.strip().split("  ")[-1])
        if section=="telephone" and "Etat" in line and not "Etat du" in line:
            info["telephonestate"]=line.strip().split("  ")[-1].strip()
        if section=="DSL" and "Etat" in line and not "Date" in line:
            info["dslstate"]=line.strip().split("  ")[-1].strip()
        if section=="DSL" and "bit ATM" in line:
            info["speeddown"]=float(arr[-2].strip().split(" ")[0])
            info["speedup"]=float(arr[-1].strip().split(" ")[0])
        if section=="DSL" and "Marge de bruit" in line:
            info["noisemargindown"]=float(arr[-2].strip().split(" ")[0])
            info["noisemarginup"]=float(arr[-1].strip().split(" ")[0]) 
        if section=="DSL" and "nuation" in line:
            info["attenuationdown"]=float(arr[-2].strip().split(" ")[0])
            info["attenuationup"]=float(arr[-1].strip().split(" ")[0])
        if section=="DSL" and "FEC" in line:
            info["FECdown"]=float(arr[-2].strip().split(" ")[0])
            info["FECup"]=float(arr[-1].strip().split(" ")[0])
        if section=="DSL" and "CRC" in line:
            info["CRCdown"]=float(arr[-2].strip().split(" ")[0])
            info["CRCup"]=float(arr[-1].strip().split(" ")[0])
        if section=="DSL" and "HEC" in line:
            info["HECdown"]=float(arr[-2].strip().split(" ")[0])
            info["HECup"]=float(arr[-1].strip().split(" ")[0])
        if section=="Wifi" and "Etat" in line and not "seau" in line:
            info["wifistate"]=line.strip().strip().split("  ")[-1].strip()
        if section=="network" and "Adrese IP" in line:
            info["ipv4"]=line.strip().strip().split("  ")[-1]
        if section=="network" and "WAN" in line:
            info["wanstate"]=arr[1].strip()
            info["wanspeeddown"]=float(arr[2].strip().split(" ")[0])
            info["wanspeedup"]=float(arr[3].strip().split(" ")[0])
        if debug:
            print(section, line, end='')

    print("INFO: ", info)
    return info

def showInternetState():
    client = InfluxDBClient(host=secrets.dbhost,
                            port=secrets.dbport,
                            username=secrets.read_username,
                            password=secrets.read_password,
                            database=secrets.database,
                            ssl=True,
                            verify_ssl=True)

    query='SELECT "firmware","uptime","telephonestate","dslstate","speeddown","speedup","noisemargindown","noisemarginup","attenuationdown","attenuationup","FECdown","FECup","CRCdown","CRCup","HECdown","HECup","wifistate","wanstate","wanspeeddown","wanspeedup" FROM "'+secrets.database+'"."autogen"."internetstate" GROUP BY "location"'
    ret = client.query(query,
                       database=secrets.database,
                       raise_errors=True,
                       chunked=False,
                       chunk_size=0,
                       method="GET")

    print(ret)

def commitInternetState(info, measurement):
    json_body = [
        {
            "measurement": measurement,
            "tags": {
                "location": "ferney-voltaire",
            },
            "fields": info
        }
    ]
    client = InfluxDBClient(host=secrets.dbhost,
                            port=secrets.dbport,
                            username=secrets.write_username,
                            password=secrets.write_password,
                            database=secrets.database,
                            ssl=True,
                            verify_ssl=True)
    ret = client.write_points(json_body)


def runSpeedtest():
    s = speedtest.Speedtest()
    servers=[]
    s.get_servers(servers)
    s.get_best_server()
    s.download(threads=None)
    s.upload(threads=None)
    resultdict=s.results.dict()
    resultdict.pop('timestamp', None)
    resultdict.pop('share', None)
    resultdict['server']=resultdict['server']['url']
    resultdict['client']=resultdict['client']['ip']
    print("Speedtest:", resultdict)
    return resultdict

def usage():
     print('showinternetstate.py [-h] [-s]')

def main(argv):
    try:
        opts, args = getopt.getopt(argv,"hds",["help","debug","speedtest"])
    except getopt.GetoptError:
        usage()
        sys.exit(2)

    debug=False

    for o, a in opts:
        if o in ("-d","--debug"):
            debug = True
        elif o in ("-h", "--help"):
            usage()
            sys.exit()
        elif o in ("-s", "--speedtest"):
            speedresult = runSpeedtest()
            commitInternetState(speedresult,"speedtest")
        else:
            assert False, "unhandled option"

    info = getInternetState(debug=debug)
    commitInternetState(info,"internetstate")
    if debug:
        showInternetState()

if __name__ == "__main__":
   main(sys.argv[1:])
