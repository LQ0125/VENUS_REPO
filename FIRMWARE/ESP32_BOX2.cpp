/*Venus Box 2*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

/* Copy the working values from the Box 1 gateway. */
const char* WIFI_SSID = "WIFI Name";
const char* WIFI_PASSWORD = "WIFI Password";
const char* MQTT_BROKER = "MQTT IP";
const uint16_t MQTT_PORT = 1883;

const char* ACTUATOR_NODE =
    "venus/living_room/actuator_node_01";
const char* ACTUATOR_TELEMETRY_TOPIC =
    "venus/living_room/actuator_node_01/telemetry";
const char* ACTUATOR_COMMAND_TOPIC =
    "venus/living_room/actuator_node_01/command";
const char* ACTUATOR_ACK_TOPIC =
    "venus/living_room/actuator_node_01/ack";


#define PIC_TX_PIN 17
#define PIC_RX_PIN 16

HardwareSerial PICSerial(2);
WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
String picLine;

enum LightMode : uint8_t
{
    LIGHT_OFF = 0,
    LIGHT_WARM_WHITE = 1,
    LIGHT_NATURAL_WHITE = 2,
    LIGHT_DAYLIGHT = 3
};

uint8_t lightModeActual = LIGHT_OFF;
bool doorOpenActual = false;
uint8_t doorAngleActual = 0;

bool pendingCommand = false;
String pendingCommandId;
String pendingTarget;
bool pendingRequestedState = false;
uint8_t pendingRequestedMode = LIGHT_OFF;
unsigned long pendingCommandStartedAt = 0UL;
const unsigned long PIC_ACK_TIMEOUT_MS = 2000UL;

unsigned long lastNetworkStatusAt = 0UL;
const unsigned long NETWORK_STATUS_INTERVAL_MS = 10000UL;

#define COMMAND_CACHE_SIZE 8

struct CommandResult
{
    String id;
    String target;
    String status;
    bool state;
    uint8_t mode;
    bool valid;
};

CommandResult commandCache[COMMAND_CACHE_SIZE];
uint8_t cacheIndex = 0;

const char* lightModeName(uint8_t mode)
{
    switch (mode)
    {
        case LIGHT_WARM_WHITE: return "warm_white";
        case LIGHT_NATURAL_WHITE: return "natural_white";
        case LIGHT_DAYLIGHT: return "daylight";
        default: return "off";
    }
}

int parseLightMode(String mode)
{
    mode.toLowerCase();
    mode.trim();
    mode.replace("-", "_");
    mode.replace(" ", "_");

    if (mode == "off") return LIGHT_OFF;
    if (mode == "warm" || mode == "warm_white" || mode == "2700k")
        return LIGHT_WARM_WHITE;
    if (mode == "natural" || mode == "natural_white" ||
        mode == "neutral_white" || mode == "3500k")
        return LIGHT_NATURAL_WHITE;
    if (mode == "daylight" || mode == "cool_white" || mode == "5000k")
        return LIGHT_DAYLIGHT;

    return -1;
}

CommandResult* findCachedCommand(const String& id)
{
    for (uint8_t i = 0; i < COMMAND_CACHE_SIZE; i++)
    {
        if (commandCache[i].valid && commandCache[i].id == id)
            return &commandCache[i];
    }

    return nullptr;
}

void rememberCommand(
    const String& id,
    const String& target,
    const String& status,
    bool state,
    uint8_t mode
)
{
    commandCache[cacheIndex] = {
        id, target, status, state, mode, true
    };

    cacheIndex = (uint8_t)((cacheIndex + 1U) % COMMAND_CACHE_SIZE);
}

void publishActuatorTelemetry()
{
    StaticJsonDocument<320> doc;

    doc["actuators"]["led"] = lightModeActual != LIGHT_OFF;
    doc["actuators"]["servo"] = doorOpenActual;

    /* Current Core ignores details, but they remain available for diagnostics. */
    doc["details"]["light_mode"] = lightModeName(lightModeActual);
    doc["details"]["door_angle"] = doorAngleActual;

    char payload[320];
    serializeJson(doc, payload);
    mqttClient.publish(ACTUATOR_TELEMETRY_TOPIC, payload);

    Serial.print("[ACTUATOR MQTT] ");
    Serial.println(payload);
}

void publishAck(
    const String& commandId,
    const String& target,
    const String& status,
    bool actualState,
    uint8_t actualMode
)
{
    StaticJsonDocument<320> doc;

    doc["command_id"] = commandId;
    doc["status"] = status;
    doc["target"] = target;
    doc["state"] = actualState;

    if (target == "led")
        doc["mode"] = lightModeName(actualMode);

    if (target == "servo" || target == "door")
        doc["angle"] = doorAngleActual;

    char payload[320];
    serializeJson(doc, payload);
    mqttClient.publish(ACTUATOR_ACK_TOPIC, payload);

    Serial.print("[ACK MQTT] ");
    Serial.println(payload);
}

void clearPending()
{
    pendingCommand = false;
    pendingCommandId = "";
    pendingTarget = "";
    pendingRequestedState = false;
    pendingRequestedMode = LIGHT_OFF;
    pendingCommandStartedAt = 0UL;
}

void processPICState(const String& line)
{
    int mode;
    int doorOpen;
    int doorAngle;

    if (sscanf(line.c_str(), "STATE,%d,%d,%d",
               &mode, &doorOpen, &doorAngle) != 3)
    {
        Serial.print("[UART] Invalid STATE packet: ");
        Serial.println(line);
        return;
    }

    if (mode < LIGHT_OFF || mode > LIGHT_DAYLIGHT)
        return;

    lightModeActual = (uint8_t)mode;
    doorOpenActual = doorOpen == 1;
    doorAngleActual = (uint8_t)doorAngle;

    Serial.print("[PIC STATE] Light=");
    Serial.print(lightModeName(lightModeActual));
    Serial.print(" Door=");
    Serial.print(doorOpenActual ? "OPEN" : "CLOSED");
    Serial.print(" Angle=");
    Serial.println(doorAngleActual);

    publishActuatorTelemetry();
}

void processPICLightAck(const String& line)
{
    int requestedMode;
    int actualMode;

    if (sscanf(line.c_str(), "ACK,L,%d,%d",
               &requestedMode, &actualMode) != 2)
        return;

    if (actualMode < LIGHT_OFF || actualMode > LIGHT_DAYLIGHT)
        return;

    lightModeActual = (uint8_t)actualMode;

    Serial.print("[PIC ACK] Target=Light Requested=");
    Serial.print(lightModeName((uint8_t)requestedMode));
    Serial.print(" Actual=");
    Serial.println(lightModeName(lightModeActual));

    publishActuatorTelemetry();

    if (!pendingCommand || pendingTarget != "led" ||
        requestedMode != pendingRequestedMode)
        return;

    bool actualState = lightModeActual != LIGHT_OFF;
    String status =
        (actualMode == pendingRequestedMode) ? "executed" : "failed";

    publishAck(
        pendingCommandId,
        pendingTarget,
        status,
        actualState,
        lightModeActual
    );

    rememberCommand(
        pendingCommandId,
        pendingTarget,
        status,
        actualState,
        lightModeActual
    );

    clearPending();
}

void processPICDoorAck(const String& line)
{
    int requestedOpen;
    int actualOpen;
    int actualAngle;

    if (sscanf(line.c_str(), "ACK,D,%d,%d,%d",
               &requestedOpen, &actualOpen, &actualAngle) != 3)
        return;

    doorOpenActual = actualOpen == 1;
    doorAngleActual = (uint8_t)actualAngle;

    Serial.print("[PIC ACK] Target=Door Requested=");
    Serial.print(requestedOpen == 1 ? "OPEN" : "CLOSED");
    Serial.print(" Actual=");
    Serial.print(doorOpenActual ? "OPEN" : "CLOSED");
    Serial.print(" Angle=");
    Serial.println(doorAngleActual);

    publishActuatorTelemetry();

    if (!pendingCommand ||
        (pendingTarget != "servo" && pendingTarget != "door") ||
        ((requestedOpen == 1) != pendingRequestedState))
        return;

    bool positionMatches =
        doorOpenActual == pendingRequestedState &&
        doorAngleActual == (pendingRequestedState ? 90U : 0U);

    String status = positionMatches ? "executed" : "failed";

    publishAck(
        pendingCommandId,
        pendingTarget,
        status,
        doorOpenActual,
        lightModeActual
    );

    rememberCommand(
        pendingCommandId,
        pendingTarget,
        status,
        doorOpenActual,
        lightModeActual
    );

    clearPending();
}

void processPICLine(String line)
{
    line.trim();
    if (line.length() == 0) return;

    Serial.print("[PIC -> ESP32] ");
    Serial.println(line);

    if (line.startsWith("STATE,"))
        processPICState(line);
    else if (line.startsWith("ACK,L,"))
        processPICLightAck(line);
    else if (line.startsWith("ACK,D,"))
        processPICDoorAck(line);
    else if (line == "PIC,BOX2_READY")
        Serial.println("[PIC] Box 2 controller ready.");
}

void failImmediately(
    const String& commandId,
    const String& target
)
{
    bool state = (target == "led")
        ? (lightModeActual != LIGHT_OFF)
        : doorOpenActual;

    publishAck(commandId, target, "failed", state, lightModeActual);
    rememberCommand(commandId, target, "failed", state, lightModeActual);
}

void handleMQTTCommand(char* topic, byte* payload, unsigned int length)
{
    (void)topic;

    StaticJsonDocument<768> doc;
    DeserializationError error = deserializeJson(doc, payload, length);

    if (error)
    {
        Serial.println("[MQTT] Invalid JSON command.");
        return;
    }

    String commandId = doc["command_id"] | "";
    String device = doc["device"]["name"] | "";
    String operation = doc["action"]["operation"] | "";

    device.toLowerCase();
    device.trim();

    /* Box 1 and Box 2 share the logical actuator topic. */
    bool isLight = device == "led";
    bool isDoor = device == "servo" || device == "door";

    if (!isLight && !isDoor)
    {
        Serial.print("[MQTT] Ignoring non-Box2 actuator: ");
        Serial.println(device);
        return;
    }

    if (commandId.length() == 0)
    {
        Serial.println("[MQTT] Box 2 command missing command_id.");
        return;
    }

    CommandResult* cached = findCachedCommand(commandId);
    if (cached != nullptr)
    {
        Serial.println("[MQTT] Duplicate command. Replaying ACK.");

        publishAck(
            cached->id,
            cached->target,
            cached->status,
            cached->state,
            cached->mode
        );
        return;
    }

    if (operation != "set" || pendingCommand)
    {
        if (pendingCommand)
            Serial.println("[MQTT] Another Box 2 command is pending.");

        failImmediately(commandId, device);
        return;
    }

    if (isLight)
    {
        int requestedMode = -1;
        JsonVariant value = doc["action"]["value"];
        String explicitMode = doc["action"]["mode"] | "";

        if (explicitMode.length() > 0)
        {
            requestedMode = parseLightMode(explicitMode);
        }
        else if (value.is<const char*>())
        {
            requestedMode = parseLightMode(value.as<String>());
        }
        else if (value.is<bool>())
        {
            /* Backward compatibility with the current Boolean Core schema. */
            requestedMode = value.as<bool>()
                ? LIGHT_NATURAL_WHITE
                : LIGHT_OFF;
        }

        if (requestedMode < LIGHT_OFF || requestedMode > LIGHT_DAYLIGHT)
        {
            failImmediately(commandId, device);
            return;
        }

        pendingCommand = true;
        pendingCommandId = commandId;
        pendingTarget = "led";
        pendingRequestedMode = (uint8_t)requestedMode;
        pendingRequestedState = requestedMode != LIGHT_OFF;
        pendingCommandStartedAt = millis();

        PICSerial.print('L');
        PICSerial.print(requestedMode);

        Serial.printf("[ESP32 -> PIC] L%d\n", requestedMode);
        return;
    }

    JsonVariant value = doc["action"]["value"];
    if (!value.is<bool>())
    {
        failImmediately(commandId, device);
        return;
    }

    bool requestedOpen = value.as<bool>();

    pendingCommand = true;
    pendingCommandId = commandId;
    pendingTarget = device;
    pendingRequestedState = requestedOpen;
    pendingRequestedMode = LIGHT_OFF;
    pendingCommandStartedAt = millis();

    PICSerial.print(requestedOpen ? "D1" : "D0");
    Serial.println(requestedOpen
        ? "[ESP32 -> PIC] D1"
        : "[ESP32 -> PIC] D0");
}

void connectWiFi()
{
    Serial.print("Connecting Wi-Fi");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    while (WiFi.status() != WL_CONNECTED)
    {
        delay(500);
        Serial.print('.');
    }

    Serial.println();
    Serial.print("Wi-Fi connected. IP: ");
    Serial.println(WiFi.localIP());
}

void connectMQTT()
{
    while (!mqttClient.connected())
    {
        Serial.print("Connecting MQTT...");
        String clientId = "venus-box2-";
        clientId += String((uint32_t)ESP.getEfuseMac(), HEX);

        if (mqttClient.connect(clientId.c_str()))
        {
            Serial.println(" connected.");
            mqttClient.subscribe(ACTUATOR_COMMAND_TOPIC, 1);
            Serial.print("Subscribed: ");
            Serial.println(ACTUATOR_COMMAND_TOPIC);
        }
        else
        {
            Serial.print(" failed rc=");
            Serial.println(mqttClient.state());
            delay(2000);
        }
    }
}

void printNetworkStatus()
{
    Serial.print("[NETWORK STATUS] Wi-Fi=");
    Serial.print(WiFi.status() == WL_CONNECTED
        ? "CONNECTED"
        : "DISCONNECTED");

    if (WiFi.status() == WL_CONNECTED)
    {
        Serial.print(" IP=");
        Serial.print(WiFi.localIP());
    }

    Serial.print(" MQTT=");
    Serial.println(mqttClient.connected()
        ? "CONNECTED"
        : "DISCONNECTED");
}

void setup()
{
    Serial.begin(115200);
    PICSerial.begin(9600, SERIAL_8N1, PIC_RX_PIN, PIC_TX_PIN);
    delay(500);

    Serial.println();
    Serial.println("======================================");
    Serial.println(" VENUS BOX 2 ESP32-S3 GATEWAY");
    Serial.println("======================================");
    Serial.println(
    "GPIO17 TX2 -> PIC pin 26 RC7/RX");
    Serial.println("GPIO16 RX2 <- PIC pin 25 RC6/TX");

    connectWiFi();

    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setCallback(handleMQTTCommand);
    mqttClient.setBufferSize(1024);
}

void loop()
{
    if (WiFi.status() != WL_CONNECTED)
        connectWiFi();

    if (!mqttClient.connected())
        connectMQTT();

    mqttClient.loop();

    if (lastNetworkStatusAt == 0UL ||
        millis() - lastNetworkStatusAt >= NETWORK_STATUS_INTERVAL_MS)
    {
        lastNetworkStatusAt = millis();
        printNetworkStatus();
    }

    while (PICSerial.available())
    {
        char c = (char)PICSerial.read();

        if (c == '\n')
        {
            processPICLine(picLine);
            picLine = "";
        }
        else if (c != '\r')
        {
            if (picLine.length() < 100)
                picLine += c;
            else
                picLine = "";
        }
    }

    if (pendingCommand &&
        millis() - pendingCommandStartedAt >= PIC_ACK_TIMEOUT_MS)
    {
        bool state = pendingTarget == "led"
            ? (lightModeActual != LIGHT_OFF)
            : doorOpenActual;

        publishAck(
            pendingCommandId,
            pendingTarget,
            "failed",
            state,
            lightModeActual
        );

        rememberCommand(
            pendingCommandId,
            pendingTarget,
            "failed",
            state,
            lightModeActual
        );

        Serial.println("[PIC ACK TIMEOUT] Box 2 command failed.");
        clearPending();
    }
}
