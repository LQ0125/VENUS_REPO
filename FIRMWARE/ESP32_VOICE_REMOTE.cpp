/*Venus Remote Control*/

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <esp_system.h>

// Replace these values with the network used by the MQTT broker.
const char* WIFI_SSID = "WIFI Name";
const char* WIFI_PASSWORD = "WIFI Password";
const char* MQTT_BROKER = "MQTT IP";
const uint16_t MQTT_PORT = 1883;

const char* VOICE_DEVICE_ID = "voice_remote_01";
const char* OPERATOR_DEVICE_ID = "operator_remote_01";

const char* VOICE_BUTTON_TOPIC =
    "venus/interface/voice_remote_01/button";
const char* MIC_STATE_TOPIC =
    "venus/interface/voice_remote_01/mic_state";
const char* VOICE_REMOTE_STATUS_TOPIC =
    "venus/interface/voice_remote_01/status";
const char* SIDECAR_STATUS_TOPIC =
    "venus/interface/voice_remote_01/sidecar_status";

const char* OPERATOR_REMOTE_STATUS_TOPIC =
    "venus/interface/operator_remote_01/status";
const char* CORE_STATUS_TOPIC =
    "venus/interface/operator_remote_01/core_status";
const char* SIMULATION_REQUEST_TOPIC =
    "venus/interface/operator_remote_01/simulation/request";
const char* SIMULATION_STATE_TOPIC =
    "venus/interface/operator_remote_01/simulation/state";
const char* LATENCY_PING_TOPIC =
    "venus/interface/operator_remote_01/latency/ping";
const char* LATENCY_PONG_TOPIC =
    "venus/interface/operator_remote_01/latency/pong";

#define MUTE_BUTTON_PIN 32
#define UP_BUTTON_PIN 25
#define DOWN_BUTTON_PIN 26
#define SELECT_BUTTON_PIN 27
#define BACK_BUTTON_PIN 33

#define LEFT_OLED_SDA 21
#define LEFT_OLED_SCL 22
#define RIGHT_OLED_SDA 18
#define RIGHT_OLED_SCL 19
#define OLED_ADDRESS 0x3C
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET_PIN -1

const unsigned long DEBOUNCE_MS = 45UL;
const unsigned long SELECT_HOLD_MS = 2000UL;
const unsigned long HEARTBEAT_MS = 5000UL;
const unsigned long NETWORK_RETRY_MS = 2000UL;
const unsigned long EVENT_RETRY_MS = 800UL;
const unsigned long LATENCY_TIMEOUT_MS = 2500UL;
const unsigned long UI_REFRESH_MS = 100UL;
const unsigned long OLED_DIM_AFTER_MS = 60000UL;
const uint8_t MAX_EVENT_ATTEMPTS = 3;
const uint8_t SIMULATION_DURATION_SECONDS = 10;

TwoWire leftOledBus = TwoWire(0);
TwoWire rightOledBus = TwoWire(1);
Adafruit_SSD1306 leftDisplay(
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    &leftOledBus,
    OLED_RESET_PIN
);
Adafruit_SSD1306 rightDisplay(
    SCREEN_WIDTH,
    SCREEN_HEIGHT,
    &rightOledBus,
    OLED_RESET_PIN
);

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);

enum MenuItem : uint8_t
{
    MENU_FIRE_SIMULATION = 0,
    MENU_GAS_SIMULATION = 1,
    MENU_LATENCY_LAB = 2,
    MENU_ITEM_COUNT = 3
};

enum SimulationUiState : uint8_t
{
    SIMULATION_IDLE,
    SIMULATION_WAITING,
    SIMULATION_ACTIVE,
    SIMULATION_CLEARED,
    SIMULATION_REJECTED,
    SIMULATION_ERROR
};

enum LatencyUiState : uint8_t
{
    LATENCY_IDLE,
    LATENCY_WAITING,
    LATENCY_RESULT,
    LATENCY_ERROR
};

struct DebouncedButton
{
    uint8_t pin;
    bool lastRaw;
    bool stable;
    unsigned long changedAt;
    unsigned long pressedAt;
};

DebouncedButton muteButton = {
    MUTE_BUTTON_PIN, HIGH, HIGH, 0UL, 0UL
};
DebouncedButton upButton = {
    UP_BUTTON_PIN, HIGH, HIGH, 0UL, 0UL
};
DebouncedButton downButton = {
    DOWN_BUTTON_PIN, HIGH, HIGH, 0UL, 0UL
};
DebouncedButton selectButton = {
    SELECT_BUTTON_PIN, HIGH, HIGH, 0UL, 0UL
};
DebouncedButton backButton = {
    BACK_BUTTON_PIN, HIGH, HIGH, 0UL, 0UL
};

bool leftDisplayReady = false;
bool rightDisplayReady = false;
bool sidecarOnline = false;
bool coreOnline = false;
bool microphoneEnabled = false;
bool uiDirty = true;
bool displaysDimmed = false;
bool pendingSimulationCancel = false;

MenuItem selectedMenu = MENU_FIRE_SIMULATION;
SimulationUiState simulationUiState = SIMULATION_IDLE;
LatencyUiState latencyUiState = LATENCY_IDLE;

String activeSimulationType;
String simulationMessage;
String pendingSimulationRequestId;
String pendingMicEventId;
String pendingLatencyRequestId;

uint32_t bootToken = 0;
uint32_t eventSequence = 0;
uint8_t pendingMicAttempts = 0;
uint8_t pendingSimulationAttempts = 0;
unsigned long simulationEndsAt = 0UL;
unsigned long pendingMicRetryAt = 0UL;
unsigned long pendingSimulationRetryAt = 0UL;
unsigned long latencySentAt = 0UL;
unsigned long lastLatencyMs = 0UL;
unsigned long lastHeartbeatAt = 0UL;
unsigned long lastNetworkAttemptAt = 0UL;
unsigned long lastUiRefreshAt = 0UL;
unsigned long lastInteractionAt = 0UL;

String makeEventId(const char* prefix)
{
    eventSequence++;
    return String(prefix)
        + "-"
        + String(bootToken, HEX)
        + "-"
        + String(eventSequence);
}

void markInteraction()
{
    lastInteractionAt = millis();
    uiDirty = true;
    if (displaysDimmed)
    {
        if (leftDisplayReady)
            leftDisplay.ssd1306_command(SSD1306_DISPLAYON);
        if (rightDisplayReady)
            rightDisplay.ssd1306_command(SSD1306_DISPLAYON);
        displaysDimmed = false;
    }
}

void printStatusValue(
    Adafruit_SSD1306& display,
    int16_t y,
    const char* label,
    const char* value
)
{
    display.setCursor(0, y);
    display.setTextColor(SSD1306_WHITE);
    display.print(label);
    int16_t valueWidth = strlen(value) * 6;
    display.setCursor(max(0, SCREEN_WIDTH - valueWidth), y);
    display.print(value);
}

void renderLeftDisplay()
{
    if (!leftDisplayReady)
        return;

    leftDisplay.clearDisplay();
    leftDisplay.setTextSize(1);
    leftDisplay.setTextColor(SSD1306_WHITE);
    leftDisplay.setCursor(0, 0);
    leftDisplay.println("V.E.N.U.S REMOTE");
    leftDisplay.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);

    printStatusValue(
        leftDisplay,
        16,
        "MIC",
        sidecarOnline
            ? (microphoneEnabled ? "LISTENING" : "MUTED")
            : "OFFLINE"
    );
    printStatusValue(
        leftDisplay,
        28,
        "WIFI",
        WiFi.status() == WL_CONNECTED ? "ONLINE" : "OFFLINE"
    );
    printStatusValue(
        leftDisplay,
        40,
        "MQTT",
        mqttClient.connected() ? "ONLINE" : "OFFLINE"
    );
    printStatusValue(
        leftDisplay,
        52,
        "CORE",
        coreOnline ? "ONLINE" : "OFFLINE"
    );
    leftDisplay.display();
}

const char* menuTitle(MenuItem item)
{
    switch (item)
    {
        case MENU_FIRE_SIMULATION:
            return "FIRE DRILL";
        case MENU_GAS_SIMULATION:
            return "GAS DRILL";
        case MENU_LATENCY_LAB:
        default:
            return "LATENCY LAB";
    }
}

void renderMenu()
{
    rightDisplay.setCursor(0, 0);
    rightDisplay.println("SELECT MODE");
    rightDisplay.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);

    for (uint8_t index = 0; index < MENU_ITEM_COUNT; index++)
    {
        int16_t y = 17 + (index * 13);
        rightDisplay.setCursor(0, y);
        rightDisplay.print(index == selectedMenu ? "> " : "  ");
        rightDisplay.println(menuTitle((MenuItem)index));
    }

    rightDisplay.setCursor(0, 56);
    if (selectedMenu == MENU_LATENCY_LAB)
        rightDisplay.print("SELECT to ping");
    else
        rightDisplay.print("Hold SELECT");
}

void renderSimulation()
{
    rightDisplay.setCursor(0, 0);
    rightDisplay.print(activeSimulationType.length() > 0
        ? activeSimulationType
        : menuTitle(selectedMenu));
    rightDisplay.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);

    rightDisplay.setCursor(0, 16);
    switch (simulationUiState)
    {
        case SIMULATION_WAITING:
            rightDisplay.println("REQUESTING...");
            rightDisplay.println();
            rightDisplay.println("PHYSICAL RESPONSE");
            break;
        case SIMULATION_ACTIVE:
        {
            unsigned long remainingMs =
                simulationEndsAt > millis()
                    ? simulationEndsAt - millis()
                    : 0UL;
            unsigned long remainingSeconds =
                (remainingMs + 999UL) / 1000UL;
            rightDisplay.println("DRILL ACTIVE");
            rightDisplay.println("ACTUATORS ACTIVE");
            rightDisplay.print("Remaining: ");
            rightDisplay.print(remainingSeconds);
            rightDisplay.println("s");
            break;
        }
        case SIMULATION_CLEARED:
            rightDisplay.println("DRILL CLEAR");
            rightDisplay.println();
            rightDisplay.println("SELECT: new test");
            break;
        case SIMULATION_REJECTED:
            rightDisplay.println("REQUEST REJECTED");
            rightDisplay.println();
            rightDisplay.println(simulationMessage);
            break;
        case SIMULATION_ERROR:
            rightDisplay.println("NO CORE RESPONSE");
            rightDisplay.println();
            rightDisplay.println("BACK: menu");
            break;
        case SIMULATION_IDLE:
        default:
            renderMenu();
            return;
    }

    rightDisplay.setCursor(0, 56);
    rightDisplay.print("BACK to cancel");
}

void renderLatency()
{
    rightDisplay.setCursor(0, 0);
    rightDisplay.println("LATENCY LAB");
    rightDisplay.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);
    rightDisplay.setCursor(0, 17);

    switch (latencyUiState)
    {
        case LATENCY_WAITING:
            rightDisplay.println("PINGING CORE...");
            rightDisplay.println();
            rightDisplay.println("MQTT round trip");
            break;
        case LATENCY_RESULT:
            rightDisplay.println("CORE ROUND TRIP");
            rightDisplay.setTextSize(2);
            rightDisplay.setCursor(0, 31);
            rightDisplay.print(lastLatencyMs);
            rightDisplay.println(" ms");
            rightDisplay.setTextSize(1);
            break;
        case LATENCY_ERROR:
            rightDisplay.println("PING TIMEOUT");
            rightDisplay.println();
            rightDisplay.println("Check Core/MQTT");
            break;
        case LATENCY_IDLE:
        default:
            rightDisplay.println("Remote > Core > Remote");
            rightDisplay.println();
            rightDisplay.println("SELECT to begin");
            break;
    }

    rightDisplay.setTextSize(1);
    rightDisplay.setCursor(0, 56);
    rightDisplay.print("BACK: menu");
}

void renderRightDisplay()
{
    if (!rightDisplayReady)
        return;

    rightDisplay.clearDisplay();
    rightDisplay.setTextSize(1);
    rightDisplay.setTextColor(SSD1306_WHITE);

    if (simulationUiState != SIMULATION_IDLE)
        renderSimulation();
    else if (
        selectedMenu == MENU_LATENCY_LAB
        && latencyUiState != LATENCY_IDLE
    )
        renderLatency();
    else
        renderMenu();

    rightDisplay.display();
}

void renderUi(bool force = false)
{
    unsigned long now = millis();
    bool countdownRefresh = simulationUiState == SIMULATION_ACTIVE;
    if (
        !force
        && !uiDirty
        && !countdownRefresh
        && now - lastUiRefreshAt < UI_REFRESH_MS
    )
        return;
    if (!force && now - lastUiRefreshAt < UI_REFRESH_MS)
        return;

    lastUiRefreshAt = now;
    uiDirty = false;
    renderLeftDisplay();
    renderRightDisplay();
}

void serviceOledSleep()
{
    if (
        !displaysDimmed
        && millis() - lastInteractionAt >= OLED_DIM_AFTER_MS
        && simulationUiState != SIMULATION_ACTIVE
        && latencyUiState != LATENCY_WAITING
    )
    {
        if (leftDisplayReady)
            leftDisplay.ssd1306_command(SSD1306_DISPLAYOFF);
        if (rightDisplayReady)
            rightDisplay.ssd1306_command(SSD1306_DISPLAYOFF);
        displaysDimmed = true;
    }
}

void applyConfirmedMicrophoneState(bool enabled)
{
    microphoneEnabled = enabled;
    uiDirty = true;
    Serial.print("[MICROPHONE] Confirmed ");
    Serial.println(enabled ? "LISTENING" : "MUTED");
}

void publishRemoteStatus()
{
    if (!mqttClient.connected())
        return;

    StaticJsonDocument<224> doc;
    doc["status"] = "online";
    doc["device_id"] = VOICE_DEVICE_ID;
    doc["operator_device_id"] = OPERATOR_DEVICE_ID;
    doc["uptime_ms"] = millis();

    char payload[224];
    serializeJson(doc, payload);
    mqttClient.publish(VOICE_REMOTE_STATUS_TOPIC, payload, true);
    mqttClient.publish(OPERATOR_REMOTE_STATUS_TOPIC, payload, true);
    lastHeartbeatAt = millis();
}

void publishPendingMicEvent()
{
    if (!mqttClient.connected() || pendingMicEventId.length() == 0)
        return;

    StaticJsonDocument<192> doc;
    doc["event"] = "BUTTON_PRESS";
    doc["device_id"] = VOICE_DEVICE_ID;
    doc["event_id"] = pendingMicEventId;

    char payload[192];
    serializeJson(doc, payload);
    if (mqttClient.publish(VOICE_BUTTON_TOPIC, payload, false))
    {
        pendingMicAttempts++;
        pendingMicRetryAt = millis() + EVENT_RETRY_MS;
        uiDirty = true;
        Serial.print("[MUTE BUTTON] Event sent: ");
        Serial.println(pendingMicEventId);
    }
}

void beginMicButtonEvent()
{
    markInteraction();
    if (!mqttClient.connected() || !sidecarOnline)
    {
        Serial.println("[MUTE BUTTON] Sidecar channel unavailable.");
        uiDirty = true;
        return;
    }

    pendingMicEventId = makeEventId("mic");
    pendingMicAttempts = 0;
    publishPendingMicEvent();
}

void publishSimulationRequest(bool cancelRequest)
{
    if (!mqttClient.connected() || pendingSimulationRequestId.length() == 0)
        return;

    StaticJsonDocument<320> doc;
    doc["request_id"] = pendingSimulationRequestId;
    doc["device_id"] = OPERATOR_DEVICE_ID;
    doc["action"] = cancelRequest ? "cancel" : "start";
    doc["simulation_type"] =
        selectedMenu == MENU_FIRE_SIMULATION ? "fire" : "gas";
    doc["duration_seconds"] = SIMULATION_DURATION_SECONDS;
    doc["actuators_enabled"] = true;

    char payload[320];
    serializeJson(doc, payload);
    if (mqttClient.publish(SIMULATION_REQUEST_TOPIC, payload, false))
    {
        pendingSimulationAttempts++;
        pendingSimulationRetryAt = millis() + EVENT_RETRY_MS;
        uiDirty = true;
    }
}

void beginSimulation()
{
    markInteraction();
    if (!mqttClient.connected() || !coreOnline)
    {
        simulationUiState = SIMULATION_ERROR;
        simulationMessage = "Core offline";
        return;
    }

    activeSimulationType =
        selectedMenu == MENU_FIRE_SIMULATION
            ? "FIRE DRILL"
            : "GAS DRILL";
    pendingSimulationRequestId = makeEventId("sim");
    pendingSimulationAttempts = 0;
    pendingSimulationCancel = false;
    simulationUiState = SIMULATION_WAITING;
    simulationMessage = "";
    publishSimulationRequest(false);
}

void cancelSimulation()
{
    if (
        simulationUiState == SIMULATION_IDLE
        || !mqttClient.connected()
    )
        return;

    pendingSimulationRequestId = makeEventId("cancel");
    pendingSimulationAttempts = 0;
    pendingSimulationCancel = true;
    simulationUiState = SIMULATION_WAITING;
    publishSimulationRequest(true);
}

void beginLatencyTest()
{
    markInteraction();
    if (!mqttClient.connected() || !coreOnline)
    {
        latencyUiState = LATENCY_ERROR;
        return;
    }

    pendingLatencyRequestId = makeEventId("ping");
    latencySentAt = millis();
    latencyUiState = LATENCY_WAITING;

    StaticJsonDocument<192> doc;
    doc["request_id"] = pendingLatencyRequestId;
    doc["device_id"] = OPERATOR_DEVICE_ID;
    doc["remote_sent_ms"] = latencySentAt;

    char payload[192];
    serializeJson(doc, payload);
    if (!mqttClient.publish(LATENCY_PING_TOPIC, payload, false))
        latencyUiState = LATENCY_ERROR;
    uiDirty = true;
}

void handleMicState(byte* payload, unsigned int length)
{
    StaticJsonDocument<384> doc;
    if (deserializeJson(doc, payload, length))
        return;

    bool enabled = doc["microphone_enabled"] | false;
    const char* ackEventId = doc["ack_event_id"] | "";
    if (
        pendingMicEventId.length() > 0
        && strlen(ackEventId) > 0
        && pendingMicEventId == ackEventId
    )
    {
        pendingMicEventId = "";
        pendingMicAttempts = 0;
    }
    applyConfirmedMicrophoneState(enabled);
}

void handleSidecarStatus(byte* payload, unsigned int length)
{
    StaticJsonDocument<192> doc;
    if (deserializeJson(doc, payload, length))
        return;
    String status = doc["status"] | "offline";
    status.toLowerCase();
    sidecarOnline = status == "online";
    if (!sidecarOnline)
    {
        pendingMicEventId = "";
        pendingMicAttempts = 0;
    }
    uiDirty = true;
}

void handleCoreStatus(byte* payload, unsigned int length)
{
    StaticJsonDocument<192> doc;
    if (deserializeJson(doc, payload, length))
        return;
    String status = doc["status"] | "offline";
    status.toLowerCase();
    coreOnline = status == "online";
    uiDirty = true;
}

void handleSimulationState(byte* payload, unsigned int length)
{
    StaticJsonDocument<512> doc;
    if (deserializeJson(doc, payload, length))
        return;

    const char* status = doc["status"] | "rejected";
    const char* requestId = doc["request_id"] | "";
    const char* type = doc["simulation_type"] | "unknown";
    bool actuatorsEnabled = doc["actuators_enabled"] | false;
    bool simulated = doc["simulated"] | false;
    bool drill = doc["drill"] | false;

    // A response is accepted only when Core identifies it as a simulated drill
    // and explicitly confirms that physical drill actuation is enabled.
    if (!simulated || !drill || !actuatorsEnabled)
    {
        simulationUiState = SIMULATION_REJECTED;
        simulationMessage = "Invalid drill state";
        uiDirty = true;
        return;
    }

    bool matchesPendingRequest = (
        pendingSimulationRequestId.length() > 0
        && pendingSimulationRequestId == requestId
    );
    if (matchesPendingRequest)
    {
        pendingSimulationRequestId = "";
        pendingSimulationAttempts = 0;
        pendingSimulationCancel = false;
    }

    String normalizedStatus = status;
    normalizedStatus.toLowerCase();
    String normalizedType = type;
    normalizedType.toUpperCase();

    // Ignore retained completion messages from an earlier session. An active
    // retained state is accepted so a rebooted remote can recover its display.
    if (!matchesPendingRequest && normalizedStatus != "active")
        return;

    if (normalizedType != "NONE")
        activeSimulationType = normalizedType + " DRILL";

    if (normalizedStatus == "active" || normalizedStatus == "duplicate")
    {
        simulationUiState = SIMULATION_ACTIVE;
        uint8_t duration = doc["duration_seconds"] | SIMULATION_DURATION_SECONDS;
        simulationEndsAt = millis() + ((unsigned long)duration * 1000UL);
    }
    else if (normalizedStatus == "cleared")
    {
        simulationUiState = SIMULATION_CLEARED;
        simulationEndsAt = 0UL;
    }
    else
    {
        simulationUiState = SIMULATION_REJECTED;
        const char* reason = doc["reason"] | "Rejected";
        simulationMessage = String(reason);
    }
    uiDirty = true;
}

void handleLatencyPong(byte* payload, unsigned int length)
{
    StaticJsonDocument<320> doc;
    if (deserializeJson(doc, payload, length))
        return;
    const char* requestId = doc["request_id"] | "";
    const char* status = doc["status"] | "";
    if (
        pendingLatencyRequestId.length() == 0
        || pendingLatencyRequestId != requestId
        || String(status) != "pong"
    )
        return;

    lastLatencyMs = millis() - latencySentAt;
    pendingLatencyRequestId = "";
    latencyUiState = LATENCY_RESULT;
    uiDirty = true;
    Serial.print("[LATENCY] Core round trip: ");
    Serial.print(lastLatencyMs);
    Serial.println(" ms");
}

void mqttCallback(char* topic, byte* payload, unsigned int length)
{
    String topicName = topic;
    if (topicName == MIC_STATE_TOPIC)
        handleMicState(payload, length);
    else if (topicName == SIDECAR_STATUS_TOPIC)
        handleSidecarStatus(payload, length);
    else if (topicName == CORE_STATUS_TOPIC)
        handleCoreStatus(payload, length);
    else if (topicName == SIMULATION_STATE_TOPIC)
        handleSimulationState(payload, length);
    else if (topicName == LATENCY_PONG_TOPIC)
        handleLatencyPong(payload, length);
}

void connectWiFi()
{
    if (WiFi.status() == WL_CONNECTED)
        return;

    Serial.print("Connecting Wi-Fi");
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    unsigned long startedAt = millis();
    while (
        WiFi.status() != WL_CONNECTED
        && millis() - startedAt < 12000UL
    )
    {
        renderUi(true);
        delay(150);
        Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED)
    {
        Serial.print("\nWi-Fi connected. IP: ");
        Serial.println(WiFi.localIP());
    }
    else
    {
        Serial.println("\nWi-Fi connection timed out.");
    }
    uiDirty = true;
}

void connectMqtt()
{
    if (WiFi.status() != WL_CONNECTED || mqttClient.connected())
        return;

    String clientId = "venus-operator-remote-" + String(bootToken, HEX);
    bool connected = mqttClient.connect(
        clientId.c_str(),
        VOICE_REMOTE_STATUS_TOPIC,
        1,
        true,
        "offline"
    );

    if (!connected)
    {
        Serial.print("MQTT connection failed. state=");
        Serial.println(mqttClient.state());
        return;
    }

    Serial.println("MQTT connected.");
    mqttClient.subscribe(MIC_STATE_TOPIC, 1);
    mqttClient.subscribe(SIDECAR_STATUS_TOPIC, 1);
    mqttClient.subscribe(CORE_STATUS_TOPIC, 1);
    mqttClient.subscribe(SIMULATION_STATE_TOPIC, 1);
    mqttClient.subscribe(LATENCY_PONG_TOPIC, 1);
    publishRemoteStatus();
    uiDirty = true;
}

void serviceNetwork()
{
    if (WiFi.status() != WL_CONNECTED || !mqttClient.connected())
    {
        sidecarOnline = false;
        coreOnline = false;
        if (millis() - lastNetworkAttemptAt >= NETWORK_RETRY_MS)
        {
            lastNetworkAttemptAt = millis();
            connectWiFi();
            connectMqtt();
        }
        return;
    }

    mqttClient.loop();
    if (millis() - lastHeartbeatAt >= HEARTBEAT_MS)
        publishRemoteStatus();
}

void handleButtonPressed(DebouncedButton& button)
{
    markInteraction();
    if (button.pin == MUTE_BUTTON_PIN)
    {
        beginMicButtonEvent();
    }
    else if (button.pin == UP_BUTTON_PIN)
    {
        if (simulationUiState != SIMULATION_IDLE)
            return;
        selectedMenu = (MenuItem)(
            selectedMenu == 0
                ? MENU_ITEM_COUNT - 1
                : selectedMenu - 1
        );
        latencyUiState = LATENCY_IDLE;
    }
    else if (button.pin == DOWN_BUTTON_PIN)
    {
        if (simulationUiState != SIMULATION_IDLE)
            return;
        selectedMenu = (MenuItem)((selectedMenu + 1) % MENU_ITEM_COUNT);
        latencyUiState = LATENCY_IDLE;
    }
    else if (button.pin == SELECT_BUTTON_PIN)
    {
        if (
            selectedMenu == MENU_LATENCY_LAB
            && simulationUiState == SIMULATION_IDLE
        )
            beginLatencyTest();
    }
    else if (button.pin == BACK_BUTTON_PIN)
    {
        if (
            simulationUiState == SIMULATION_ACTIVE
            || simulationUiState == SIMULATION_WAITING
        )
            cancelSimulation();
        else
        {
            simulationUiState = SIMULATION_IDLE;
            latencyUiState = LATENCY_IDLE;
            activeSimulationType = "";
            simulationMessage = "";
        }
    }
}

void handleButtonReleased(
    DebouncedButton& button,
    unsigned long pressDuration
)
{
    if (
        button.pin == SELECT_BUTTON_PIN
        && selectedMenu != MENU_LATENCY_LAB
        && simulationUiState != SIMULATION_ACTIVE
        && pressDuration >= SELECT_HOLD_MS
    )
        beginSimulation();
}

void serviceButton(DebouncedButton& button)
{
    bool raw = digitalRead(button.pin);
    if (raw != button.lastRaw)
    {
        button.lastRaw = raw;
        button.changedAt = millis();
    }

    if (
        millis() - button.changedAt >= DEBOUNCE_MS
        && raw != button.stable
    )
    {
        button.stable = raw;
        if (button.stable == LOW)
        {
            button.pressedAt = millis();
            handleButtonPressed(button);
        }
        else
        {
            unsigned long duration = millis() - button.pressedAt;
            handleButtonReleased(button, duration);
        }
    }
}

void serviceButtons()
{
    serviceButton(muteButton);
    serviceButton(upButton);
    serviceButton(downButton);
    serviceButton(selectButton);
    serviceButton(backButton);
}

void servicePendingEvents()
{
    unsigned long now = millis();

    if (
        pendingMicEventId.length() > 0
        && mqttClient.connected()
        && (long)(now - pendingMicRetryAt) >= 0
    )
    {
        if (pendingMicAttempts < MAX_EVENT_ATTEMPTS)
            publishPendingMicEvent();
        else
        {
            pendingMicEventId = "";
            pendingMicAttempts = 0;
            Serial.println("[MUTE BUTTON] No sidecar acknowledgement.");
        }
    }

    if (
        pendingSimulationRequestId.length() > 0
        && mqttClient.connected()
        && (long)(now - pendingSimulationRetryAt) >= 0
    )
    {
        if (pendingSimulationAttempts < MAX_EVENT_ATTEMPTS)
            publishSimulationRequest(pendingSimulationCancel);
        else
        {
            pendingSimulationRequestId = "";
            pendingSimulationAttempts = 0;
            pendingSimulationCancel = false;
            simulationUiState = SIMULATION_ERROR;
            uiDirty = true;
        }
    }

    if (
        latencyUiState == LATENCY_WAITING
        && now - latencySentAt >= LATENCY_TIMEOUT_MS
    )
    {
        pendingLatencyRequestId = "";
        latencyUiState = LATENCY_ERROR;
        uiDirty = true;
    }
}

void initializeDisplays()
{
    leftOledBus.begin(LEFT_OLED_SDA, LEFT_OLED_SCL, 400000UL);
    rightOledBus.begin(RIGHT_OLED_SDA, RIGHT_OLED_SCL, 400000UL);

    leftDisplayReady = leftDisplay.begin(
        SSD1306_SWITCHCAPVCC,
        OLED_ADDRESS,
        false,
        false
    );
    rightDisplayReady = rightDisplay.begin(
        SSD1306_SWITCHCAPVCC,
        OLED_ADDRESS,
        false,
        false
    );

    Serial.print("Left OLED: ");
    Serial.println(leftDisplayReady ? "READY" : "NOT FOUND");
    Serial.print("Right OLED: ");
    Serial.println(rightDisplayReady ? "READY" : "NOT FOUND");

    if (leftDisplayReady)
    {
        leftDisplay.clearDisplay();
        leftDisplay.setTextColor(SSD1306_WHITE);
        leftDisplay.setTextSize(1);
        leftDisplay.setCursor(0, 0);
        leftDisplay.println("V.E.N.U.S REMOTE");
        leftDisplay.println();
        leftDisplay.println("Starting...");
        leftDisplay.display();
    }
    if (rightDisplayReady)
    {
        rightDisplay.clearDisplay();
        rightDisplay.setTextColor(SSD1306_WHITE);
        rightDisplay.setTextSize(1);
        rightDisplay.setCursor(0, 0);
        rightDisplay.println("OPERATOR CONSOLE");
        rightDisplay.println();
        rightDisplay.println("Emergency drill");
        rightDisplay.println("Hold to actuate");
        rightDisplay.display();
    }
}

void initializeButtons()
{
    pinMode(MUTE_BUTTON_PIN, INPUT_PULLUP);
    pinMode(UP_BUTTON_PIN, INPUT_PULLUP);
    pinMode(DOWN_BUTTON_PIN, INPUT_PULLUP);
    pinMode(SELECT_BUTTON_PIN, INPUT_PULLUP);
    pinMode(BACK_BUTTON_PIN, INPUT_PULLUP);
}

void setup()
{
    Serial.begin(115200);
    delay(400);

    initializeButtons();
    initializeDisplays();

    bootToken = esp_random();
    lastInteractionAt = millis();
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);
    mqttClient.setCallback(mqttCallback);
    mqttClient.setBufferSize(768);
    mqttClient.setKeepAlive(10);

    Serial.println("========================================");
    Serial.println("VENUS DUAL-OLED OPERATOR REMOTE");
    Serial.println("Mute 32 | Up 25 | Down 26 | Select 27 | Back 33");
    Serial.println("Left OLED 21/22 | Right OLED 18/19");
    Serial.println("DRILLS: PHYSICAL RESPONSE ENABLED");
    Serial.println("========================================");

    connectWiFi();
    connectMqtt();
    renderUi(true);
}

void loop()
{
    serviceNetwork();
    serviceButtons();
    servicePendingEvents();
    serviceOledSleep();
    renderUi();
    delay(5);
}
