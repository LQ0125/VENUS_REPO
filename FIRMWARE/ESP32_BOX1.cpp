/*Venus Box 1*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID =
    "WIFI Name";


const char* WIFI_PASSWORD =
    "WIFI Password";

const char* MQTT_BROKER =
    "MQTT IP";


const uint16_t MQTT_PORT =
    1883;

const char* SENSOR_NODE =
    "venus/living_room/sensor_node_01";


const char* ACTUATOR_NODE =
    "venus/living_room/actuator_node_01";


const char* SENSOR_TELEMETRY_TOPIC =
    "venus/living_room/sensor_node_01/telemetry";


const char* ACTUATOR_TELEMETRY_TOPIC =
    "venus/living_room/actuator_node_01/telemetry";


const char* ACTUATOR_COMMAND_TOPIC =
    "venus/living_room/actuator_node_01/command";


const char* ACTUATOR_ACK_TOPIC =
    "venus/living_room/actuator_node_01/ack";


// UART
#define PIC_TX_PIN 17
#define PIC_RX_PIN 18


HardwareSerial PICSerial(1);


String picLine;


// PIC HARDWARE STATE

bool gasDetected =
    false;

bool flameDetected =
    false;

bool buzzerActual =
    false;


// DHT11

int ambientTemperature =
    0;

int ambientHumidity =
    0;

bool dhtValid =
    false;


// PENDING VENUS BUZZER COMMAND

bool pendingBuzzerCommand =
    false;

unsigned long pendingCommandStartedAt = 0UL;

const unsigned long PIC_ACK_TIMEOUT_MS = 2000UL;

String pendingCommandId;


bool pendingRequestedState =
    false;


// DUPLICATE COMMAND CACHE

#define COMMAND_CACHE_SIZE 8


struct CommandResult
{
    String id;

    String status;

    bool state;

    bool valid;
};


CommandResult commandCache[
    COMMAND_CACHE_SIZE
];


uint8_t cacheIndex =
    0;



CommandResult* findCachedCommand(
    const String& id
)
{
    for (
        uint8_t i = 0;
        i < COMMAND_CACHE_SIZE;
        i++
    )
    {
        if (
            commandCache[i].valid
            &&
            commandCache[i].id == id
        )
        {
            return &commandCache[i];
        }
    }


    return nullptr;
}



void rememberCommand(
    const String& id,
    const String& status,
    bool state
)
{
    commandCache[
        cacheIndex
    ].id =
        id;


    commandCache[
        cacheIndex
    ].status =
        status;


    commandCache[
        cacheIndex
    ].state =
        state;


    commandCache[
        cacheIndex
    ].valid =
        true;


    cacheIndex++;


    if (
        cacheIndex
        >= COMMAND_CACHE_SIZE
    )
    {
        cacheIndex =
            0;
    }
}


// MQTT CLIENT

WiFiClient wifiClient;


PubSubClient mqttClient(
    wifiClient
);


// SENSOR TELEMETRY

void publishSensorTelemetry()
{
    StaticJsonDocument<384> doc;


    doc["sensors"]["gas"] =
        gasDetected;


    doc["sensors"]["fire_detected"] =
        flameDetected;


    doc["sensors"]["temperature"] =
        ambientTemperature;


    doc["sensors"]["humidity"] =
        ambientHumidity;


    doc["sensors"]["dht_valid"] =
        dhtValid;


    char payload[384];


    serializeJson(
        doc,
        payload
    );


    mqttClient.publish(
        SENSOR_TELEMETRY_TOPIC,
        payload
    );


    Serial.print(
        "[SENSOR MQTT] "
    );


    Serial.println(
        payload
    );
}



// BUZZER TELEMETRY

void publishBuzzerTelemetry()
{
    StaticJsonDocument<192> doc;


    doc["actuators"]["buzzer"] =
        buzzerActual;


    char payload[192];


    serializeJson(
        doc,
        payload
    );


    mqttClient.publish(
        ACTUATOR_TELEMETRY_TOPIC,
        payload
    );


    Serial.print(
        "[BUZZER MQTT] "
    );


    Serial.println(
        payload
    );
}



// VENUS COMMAND ACK


void publishBuzzerAck(
    const String& commandId,
    const String& status,
    bool actualState
)
{
    StaticJsonDocument<256> doc;


    doc["command_id"] =
        commandId;


    doc["status"] =
        status;


    doc["target"] =
        "buzzer";


    doc["state"] =
        actualState;


    char payload[256];


    serializeJson(
        doc,
        payload
    );


    mqttClient.publish(
        ACTUATOR_ACK_TOPIC,
        payload
    );


    Serial.print(
        "[ACK MQTT] "
    );


    Serial.println(
        payload
    );
}



void processPICState(
    const String& line
)
{
    int gas;
    int flame;
    int buzzer;

    int temperature;
    int humidity;
    int valid;


    int parsed =
        sscanf(
            line.c_str(),
            "STATE,%d,%d,%d,%d,%d,%d",
            &gas,
            &flame,
            &buzzer,
            &temperature,
            &humidity,
            &valid
        );


    if (
        parsed != 6
    )
    {
        Serial.print(
            "[UART] Invalid STATE packet: "
        );

        Serial.println(
            line
        );

        return;
    }


    gasDetected =
        gas == 1;


    flameDetected =
        flame == 1;


    buzzerActual =
        buzzer == 1;


    ambientTemperature =
        temperature;


    ambientHumidity =
        humidity;


    dhtValid =
        valid == 1;



    // SERIAL DEBUG

    Serial.print(
        "[PIC STATE] Gas="
    );

    Serial.print(
        gasDetected
    );


    Serial.print(
        " Flame="
    );

    Serial.print(
        flameDetected
    );


    Serial.print(
        " Buzzer="
    );

    Serial.print(
        buzzerActual
    );


    Serial.print(
        " Temp="
    );

    Serial.print(
        ambientTemperature
    );


    Serial.print(
        "C Humidity="
    );

    Serial.print(
        ambientHumidity
    );


    Serial.print(
        "% DHT="
    );

    Serial.println(
        dhtValid
        ? "VALID"
        : "INVALID"
    );


    /*
     * Publish each PIC state packet.
     *
     * PIC transmits periodically so Digital Twin
     * timestamps remain fresh.
     */

    publishSensorTelemetry();

    publishBuzzerTelemetry();
}



// PIC BUZZER ACK

void processPICBuzzerAck(
    const String& line
)
{
    int requested;
    int actual;
    int emergency;


    int parsed =
        sscanf(
            line.c_str(),
            "ACK,B,%d,%d,%d",
            &requested,
            &actual,
            &emergency
        );


    if (
        parsed != 3
    )
    {
        return;
    }


    buzzerActual =
        actual == 1;


    Serial.print(
        "[PIC ACK] Requested="
    );

    Serial.print(
        requested
    );


    Serial.print(
        " Actual="
    );

    Serial.print(
        actual
    );


    Serial.print(
        " Emergency="
    );

    Serial.println(
        emergency
    );


    publishBuzzerTelemetry();


    if (
        !pendingBuzzerCommand
    )
    {
        return;
    }
    if (
        requested != 0
        &&
        requested != 1
    )
    {
        return;
    }

    bool acknowledgedRequestedState =
        requested == 1;

    if (
        acknowledgedRequestedState
        != pendingRequestedState
    )
    {
        Serial.println(
            "[PIC ACK] Ignored stale or mismatched ACK."
        );

        return;
    }


    if (
        buzzerActual
        ==
        pendingRequestedState
    )
    {
        publishBuzzerAck(
            pendingCommandId,
            "executed",
            buzzerActual
        );


        rememberCommand(
            pendingCommandId,
            "executed",
            buzzerActual
        );
    }


    else
    {
        /*
         * Example:
         *
         * Venus requested buzzer OFF,
         * but PIC local emergency reflex
         * requires it to remain ON.
         */

        publishBuzzerAck(
            pendingCommandId,
            "failed",
            buzzerActual
        );


        rememberCommand(
            pendingCommandId,
            "failed",
            buzzerActual
        );
    }


    pendingBuzzerCommand =
        false;


    pendingCommandId =
        "";

    pendingCommandStartedAt = 0UL;
}



// PIC UART LINE

void processPICLine(
    String line
)
{
    line.trim();


    if (
        line.length() == 0
    )
    {
        return;
    }


    Serial.print(
        "[PIC -> ESP32] "
    );


    Serial.println(
        line
    );


    if (
        line.startsWith(
            "STATE,"
        )
    )
    {
        processPICState(
            line
        );

        return;
    }


    if (
        line.startsWith(
            "ACK,B,"
        )
    )
    {
        processPICBuzzerAck(
            line
        );

        return;
    }


    if (
        line ==
        "PIC,BOX1_READY"
    )
    {
        Serial.println(
            "[PIC] Box 1 controller ready."
        );
    }
}



// MQTT COMMAND CALLBACK

void handleMQTTCommand(
    char* topic,
    byte* payload,
    unsigned int length
)
{
    StaticJsonDocument<768> doc;


    DeserializationError error =
        deserializeJson(
            doc,
            payload,
            length
        );


    if (error)
    {
        Serial.println(
            "[MQTT] Invalid JSON command."
        );

        return;
    }


    String commandId =
        doc["command_id"]
        | "";


    String device =
        doc["device"]["name"]
        | "";


    String operation =
        doc["action"]["operation"]
        | "";


    bool value =
        doc["action"]["value"]
        | false;


    if (
        device != "buzzer"
    )
    {
        Serial.print(
            "[MQTT] Ignoring non-Box1 actuator: "
        );


        Serial.println(
            device
        );


        return;
    }


    if (
        commandId.length() == 0
    )
    {
        Serial.println(
            "[MQTT] Buzzer command missing command_id."
        );

        return;
    }


    CommandResult* cached =
        findCachedCommand(
            commandId
        );


    if (
        cached != nullptr
    )
    {
        Serial.println(
            "[MQTT] Duplicate command. Replaying ACK."
        );


        publishBuzzerAck(
            cached->id,
            cached->status,
            cached->state
        );


        return;
    }


    if (
        pendingBuzzerCommand
        &&
        pendingCommandId
        ==
        commandId
    )
    {
        return;
    }


    if (
        operation != "set"
    )
    {
        publishBuzzerAck(
            commandId,
            "failed",
            buzzerActual
        );


        rememberCommand(
            commandId,
            "failed",
            buzzerActual
        );


        return;
    }


    if (
        pendingBuzzerCommand
    )
    {
        Serial.println(
            "[MQTT] Another buzzer command is pending."
        );

        return;
    }


    pendingBuzzerCommand =
        true;

    pendingCommandId =
        commandId;

    pendingRequestedState =
        value;

    pendingCommandStartedAt =
    millis();

    // FORWARD TO PIC

    if (value)
    {
        PICSerial.print(
            "B1"
        );


        Serial.println(
            "[ESP32 -> PIC] B1"
        );
    }

    else
    {
        PICSerial.print(
            "B0"
        );


        Serial.println(
            "[ESP32 -> PIC] B0"
        );
    }
}



// WIFI

void connectWiFi()
{
    Serial.print(
        "Connecting Wi-Fi"
    );


    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD
    );


    while (
        WiFi.status()
        != WL_CONNECTED
    )
    {
        delay(500);

        Serial.print(".");
    }


    Serial.println();


    Serial.print(
        "Wi-Fi connected. IP: "
    );


    Serial.println(
        WiFi.localIP()
    );
}



// MQTT


void connectMQTT()
{
    while (
        !mqttClient.connected()
    )
    {
        Serial.print(
            "Connecting MQTT..."
        );


        String clientId =
            "venus-box1-";


        clientId +=
            String(
                (uint32_t)ESP.getEfuseMac(),
                HEX
            );


        if (
            mqttClient.connect(
                clientId.c_str()
            )
        )
        {
            Serial.println(
                " connected."
            );


            mqttClient.subscribe(
                ACTUATOR_COMMAND_TOPIC,
                1
            );


            Serial.print(
                "Subscribed: "
            );


            Serial.println(
                ACTUATOR_COMMAND_TOPIC
            );
        }

        else
        {
            Serial.print(
                " failed rc="
            );


            Serial.println(
                mqttClient.state()
            );


            delay(2000);
        }
    }
}



// SETUP

void setup()
{
    Serial.begin(
        115200
    );


    PICSerial.begin(
        9600,
        SERIAL_8N1,
        PIC_RX_PIN,
        PIC_TX_PIN
    );


    delay(500);


    Serial.println();
    Serial.println(
        "======================================"
    );

    Serial.println(
        " VENUS BOX 1 ESP32-S3 GATEWAY"
    );

    Serial.println(
        "======================================"
    );


    Serial.println(
        "GPIO17 TX -> PIC pin 26 RC7/RX"
    );


    Serial.println(
        "GPIO18 RX <- PIC pin 25 RC6/TX"
    );


    Serial.println(
        "DHT11 -> PIC RB3 pin 36"
    );


    connectWiFi();


    mqttClient.setServer(
        MQTT_BROKER,
        MQTT_PORT
    );


    mqttClient.setCallback(
        handleMQTTCommand
    );


    mqttClient.setBufferSize(
        1024
    );
}



// LOOP

void loop()
{
    if (
        WiFi.status()
        != WL_CONNECTED
    )
    {
        connectWiFi();
    }

    if (
        !mqttClient.connected()
    )
    {
        connectMQTT();
    }

    mqttClient.loop();




    while (
        PICSerial.available()
    )
    {
        char c =
            (char)PICSerial.read();

        if (
            c == '\n'
        )
        {
            processPICLine(
                picLine
            );

            picLine =
                "";
        }

        else if (
            c != '\r'
        )
        {
            if (
                picLine.length()
                < 100
            )
            {
                picLine +=
                    c;
            }

            else
            {
                picLine =
                    "";
            }
        }
    }



    if (
        pendingBuzzerCommand
        &&
        (
            millis()
            - pendingCommandStartedAt
            >= PIC_ACK_TIMEOUT_MS
        )
    )
    {
        Serial.println(
            "[PIC ACK TIMEOUT] Buzzer command failed."
        );

        publishBuzzerAck(
            pendingCommandId,
            "failed",
            buzzerActual
        );

        rememberCommand(
            pendingCommandId,
            "failed",
            buzzerActual
        );

        pendingBuzzerCommand =
            false;

        pendingCommandId =
            "";

        pendingCommandStartedAt =
            0UL;
    }
}