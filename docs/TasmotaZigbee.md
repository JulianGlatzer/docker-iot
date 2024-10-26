# Tasmota on Sonoff Zigbee Bridge

## OTA Update
use http://ota.tasmota.com/tasmota/release/tasmota-zbbridge.bin.gz from https://ota.tasmota.com/tasmota/release/

## Lixee ZLinky TIC

### Configuration for Lixee ZLinky TIC
see https://github.com/fairecasoimeme/Zlinky_TIC/tree/master

|Name	|Cluster	|Attribute |RO/RW/RP		|type	|unit	|Designation|
|---|---|---|---|---|---|---|
|HCHC 	|0x0702 	|0x0100 	|RP 	|		|Uint48 	|Wh 	|Index HCHC|
|HCHP 	|0x0702 	|0x0102 	|RP 	|		|Uint48 	|Wh 	|Index HCHP|
|IINST 	|0x0B04 	|0x0508 	|RP 	|		|Uint16 	|A 	|Courant efficace|
|IINST1 	|0x0B04 	|0x0508 	|RP 	|Triphasé 	|Uint16 	|A 	|Courant efficace phase 1|
|IINST2 	|0x0B04 	|0x0908 	|RP 	|Triphasé 	|Uint16 	|A 	|Courant efficace phase 2|
|IINST3 	|0x0B04 	|0x0A08 	|RP 	|Triphasé 	|Uint16 	|A 	|Courant efficace phase 3|
|IMAX 	|0x0B04 	|0x050A 	|RO 	|		|Uint16 	|A 	|Intensité maximale|
|IMAX1 	|0x0B04 	|0x050A 	|RO 	|Triphasé 	|Uint16 	|A 	|Intensité maximale phase 1|
|IMAX2 	|0x0B04 	|0x090A 	|RO 	|Triphasé 	|Uint16 	|A 	|Intensité maximale phase 2|
|IMAX3 	|0x0B04 	|0x0A0A 	|RO 	|Triphasé 	|Uint16 	|A 	|Intensité maximale phase 3|
|PMAX 	|0x0B04 	|0x050D 	|RO 	|Triphasé 	|Uint16 	|W 	|Puissance maximale triphasée atteinte|
|PAPP 	|0x0B04 	|0x050F 	|RP 	|Triphasé 	|Uint16 	|VA 	|Puissance apparente|
|PTEC 	|0x0702 	|0x0020 	|RO 	|		|String 	|- 	|Periode tarifaire en cours|
|ADPS 	|0xFF66 	|0x0005 	|RP 	|		|Uint16 	|A 	|Avertissement de Dépassement De Puissance Souscrite|
|ADIR1 	|0xFF66 	|0x0006 	|RP 	|Triphasé 	|Uint16 	|A 	|Avertissement de Dépassement D'intensité phase 1|
|ADIR2 	|0xFF66 	|0x0007 	|RP 	|Triphasé 	|Uint16 	|A 	|Avertissement de Dépassement D'intensité phase 2|
|ADIR3 	|0xFF66 	|0x0008 	|RP 	|Triphasé 	|Uint16 	|A 	|Avertissement de Dépassement D'intensité phase 3|

### How to read value? (Cluster=0702, command=00=read, attribute=0100 or 0102 in little endian)
	ZbSend {"Device":"0x99A2","Send":"0702_00/0001"}
	ZbSend {"Device":"0x99A2","Send":"0702_00/0201"}

	ZbSend {"Device":"0x99A2","Send":"0702_00/2000"}
	ZbSend {"Device":"0x99A2","Send":"0B04_00/0F05"}
	ZbSend {"Device":"0x99A2","Send":"0B04_00/0F05"}
	ZbSend { "device":"0x4773", "endpoint":"0x03", "cluster":"0x0B04", "read":"0x0F05" }


### How to setup reporting for RP attributes?
	ZbSend {"Device":"0x99A2","Config":{"0702/0100%25":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1000}}}
	ZbSend {"Device":"0x99A2","Config":{"0702/0102%25":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1000}}}

	ZbSend {"Device":"0x99A2","Config":{"HCHC":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"HCHP":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1}}}
	ZbSend {"Device":"0x99A2","Config":{"IINST1":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"IINST2":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"IINST3":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"PAPP":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1}}}
	ZbSend {"Device":"0x99A2","Config":{"ADPS":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"ADIR1":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"ADIR2":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1},"ADIR3":{"MinInterval":10,"MaxInterval":120,"ReportableChange":1}}}

### Other commands
	ZbStatus3 0x99A2
	ZbName 0xD35C,Eingang
	ZbPing 0xA329
	ZbForget 0xA5CE
	ZbLoad lixee_zlinky.zb

### zb file
	#Z2Tv1
	# Lixee ZLinky TIC
	:ZLinky_TIC,LiXee
	0702/0100%uint48,HCHC
	0702/0102%uint48,HCHP
	0B04/0508%uint16,IINST1
	0B04/0908%uint16,IINST2
	0B04/0A08%uint16,IINST3
	0B04/050A%uint16,IMAX1
	0B04/090A%uint16,IMAX2
	0B04/0A0A%uint16,IMAX3
	0B04/050D%uint16,PMAX
	0B04/050F%uint16,PAPP
	0702/0020%string,PTEC
	FF66/0005%uint16,ADPS
	FF66/0006%uint16,ADIR1
	FF66/0007%uint16,ADIR2
	FF66/0008%uint16,ADIR3


How to assign to a different field?
	0702/0100%25=0006/0000

