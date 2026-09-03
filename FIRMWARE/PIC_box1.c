/*
VENUS CPS - BOX 1 PIC18F4520

MQ2 DO: A0 / physical pin 2

KY026 DO: RB1 / physical pin 34

DHT11 DATA: B3 / physical pin 36

Active buzzer: RC0 / physical pin 15

UART:
RC6/TX / pin 25 -> ESP32 GPIO18 RX
RC7/RX / pin 26 <- ESP32 GPIO17 TX
 
ART: 600 baud, 8N1
*/

#pragma config OSC = INTIO67
#pragma config WDT = OFF
#pragma config LVP = OFF
#pragma config PBADEN = OFF
#pragma config MCLRE = ON

#include <xc.h>
#include <stdint.h>

#define _XTAL_FREQ 8000000UL


// ============================================================
// HARDWARE
// ============================================================

#define MQ2_PORT       PORTAbits.RA0

#define FLAME_PORT     PORTBbits.RB1

#define DHT_PORT       PORTBbits.RB3
#define DHT_LAT        LATBbits.LATB3
#define DHT_TRIS       TRISBbits.TRISB3

#define BUZZER_LAT     LATCbits.LATC0
#define BUZZER_TRIS    TRISCbits.TRISC0


// ============================================================
// SENSOR POLARITY
// ============================================================

#define MQ2_ACTIVE_LOW       1
#define FLAME_ACTIVE_LOW     1

#define BUZZER_ACTIVE_HIGH   1


// ============================================================
// SYSTEM STATE
// ============================================================

volatile uint8_t gas_triggered = 0U;
volatile uint8_t flame_triggered = 0U;

volatile uint8_t venus_buzzer_request = 0U;

volatile uint8_t emergency_active = 0U;
volatile uint8_t buzzer_actual = 0U;


// DHT11 state

volatile uint8_t dht_temperature = 0U;
volatile uint8_t dht_humidity = 0U;
volatile uint8_t dht_valid = 0U;

// ============================================================
// UART
// ============================================================

void UART_Init(uint32_t baudrate)
{
    uint16_t spbrg_value;

    spbrg_value =
        (uint16_t)(
            (_XTAL_FREQ /
            (4UL * baudrate))
            - 1UL
        );

    // RC6 TX
    TRISCbits.TRISC6 = 0U;

    // RC7 RX
    TRISCbits.TRISC7 = 1U;


    TXSTA = 0x24;
    RCSTA = 0x90;

    BAUDCONbits.BRG16 = 1U;


    SPBRG =
        (uint8_t)(
            spbrg_value & 0xFF
        );

    SPBRGH =
        (uint8_t)(
            (spbrg_value >> 8)
            & 0xFF
        );
}


void UART_Write(char data)
{
    while (!PIR1bits.TXIF)
    {
    }

    TXREG = data;
}


void UART_Write_String(
    const char *text
)
{
    while (*text)
    {
        UART_Write(*text++);
    }
}


uint8_t UART_Available(void)
{
    if (RCSTAbits.OERR)
    {
        RCSTAbits.CREN = 0U;
        RCSTAbits.CREN = 1U;
    }

    return PIR1bits.RCIF;
}


char UART_Read(void)
{
    return RCREG;
}


// ============================================================
// WRITE BYTE AS DECIMAL ASCII
// ============================================================

void UART_Write_U8(uint8_t value)
{
    if (value >= 100U)
    {
        UART_Write(
            (char)(
                '0' +
                (value / 100U)
            )
        );

        value %= 100U;

        UART_Write(
            (char)(
                '0' +
                (value / 10U)
            )
        );

        UART_Write(
            (char)(
                '0' +
                (value % 10U)
            )
        );
    }

    else if (value >= 10U)
    {
        UART_Write(
            (char)(
                '0' +
                (value / 10U)
            )
        );

        UART_Write(
            (char)(
                '0' +
                (value % 10U)
            )
        );
    }

    else
    {
        UART_Write(
            (char)(
                '0' + value
            )
        );
    }
}


// ============================================================
// BUZZER
// ============================================================

void Set_Buzzer_Output(
    uint8_t state
)
{
#if BUZZER_ACTIVE_HIGH

    BUZZER_LAT =
        state ? 1U : 0U;

#else

    BUZZER_LAT =
        state ? 0U : 1U;

#endif
}


// ============================================================
// MQ2
// ============================================================

uint8_t Read_MQ2(void)
{
#if MQ2_ACTIVE_LOW

    return (
        MQ2_PORT == 0U
    )
    ? 1U
    : 0U;

#else

    return (
        MQ2_PORT != 0U
    )
    ? 1U
    : 0U;

#endif
}


// ============================================================
// KY026
// ============================================================

uint8_t Read_Flame(void)
{
#if FLAME_ACTIVE_LOW

    return (
        FLAME_PORT == 0U
    )
    ? 1U
    : 0U;

#else

    return (
        FLAME_PORT != 0U
    )
    ? 1U
    : 0U;

#endif
}


// ============================================================
// DHT11 LOW-LEVEL TIMING
// ============================================================

uint8_t DHT_WaitWhile(
    uint8_t level,
    uint16_t timeout_us
)
{
    while (
        (uint8_t)DHT_PORT
        == level
    )
    {
        if (timeout_us == 0U)
        {
            return 0U;
        }

        __delay_us(1);

        timeout_us--;
    }

    return 1U;
}


// ============================================================
// DHT11 READ + CHECKSUM PROCESSING
// ============================================================

uint8_t DHT11_Read(
    uint8_t *temperature,
    uint8_t *humidity
)
{
    uint8_t data[5] =
    {
        0U,
        0U,
        0U,
        0U,
        0U
    };

    uint8_t byte_index;
    uint8_t bit_index;


    // --------------------------------------------------------
    // MCU START SIGNAL
    // --------------------------------------------------------

    DHT_LAT = 0U;
    DHT_TRIS = 0U;

    __delay_ms(20);


    // Release line.

    DHT_LAT = 1U;
    DHT_TRIS = 1U;

    __delay_us(30);


    // --------------------------------------------------------
    // SENSOR RESPONSE
    // --------------------------------------------------------

    if (
        !DHT_WaitWhile(
            1U,
            120U
        )
    )
    {
        return 0U;
    }


    if (
        !DHT_WaitWhile(
            0U,
            120U
        )
    )
    {
        return 0U;
    }


    if (
        !DHT_WaitWhile(
            1U,
            120U
        )
    )
    {
        return 0U;
    }


    // --------------------------------------------------------
    // READ 40 BITS
    // --------------------------------------------------------

    for (
        byte_index = 0U;
        byte_index < 5U;
        byte_index++
    )
    {
        for (
            bit_index = 0U;
            bit_index < 8U;
            bit_index++
        )
        {
            // Wait for LOW portion to end.

            if (
                !DHT_WaitWhile(
                    0U,
                    100U
                )
            )
            {
                return 0U;
            }


            /*
             * DHT11:
             *
             * short HIGH ≈ bit 0
             * long HIGH  ≈ bit 1
             *
             * Sample after ~35 us.
             */

            __delay_us(35);


            data[byte_index] <<= 1;


            if (DHT_PORT)
            {
                data[byte_index] |= 1U;


                // Wait for HIGH pulse to end.

                if (
                    !DHT_WaitWhile(
                        1U,
                        100U
                    )
                )
                {
                    return 0U;
                }
            }
        }
    }


    // --------------------------------------------------------
    // CHECKSUM
    // --------------------------------------------------------

    if (
        (uint8_t)(
            data[0]
            +
            data[1]
            +
            data[2]
            +
            data[3]
        )
        != data[4]
    )
    {
        return 0U;
    }


    // DHT11 integer values.

    *humidity =
        data[0];

    *temperature =
        data[2];


    return 1U;
}


// ============================================================
// LOCAL EMERGENCY REFLEX
// ============================================================

void Update_Safety_State(void)
{
    gas_triggered =
        Read_MQ2();


    flame_triggered =
        Read_Flame();


    emergency_active =
        (
            gas_triggered
            ||
            flame_triggered
        )
        ? 1U
        : 0U;


    /*
     * Emergency has priority over
     * Venus manual OFF.
     */

    buzzer_actual =
        (
            emergency_active
            ||
            venus_buzzer_request
        )
        ? 1U
        : 0U;


    Set_Buzzer_Output(
        buzzer_actual
    );
}


// ============================================================
// PIC -> ESP32 STATE
// ============================================================

void UART_Send_State(void)
{
    /*
     * STATE,
     * gas,
     * flame,
     * buzzer,
     * temperature,
     * humidity,
     * dht_valid
     */

    UART_Write_String(
        "STATE,"
    );


    UART_Write(
        gas_triggered
        ? '1'
        : '0'
    );

    UART_Write(',');


    UART_Write(
        flame_triggered
        ? '1'
        : '0'
    );

    UART_Write(',');


    UART_Write(
        buzzer_actual
        ? '1'
        : '0'
    );

    UART_Write(',');


    UART_Write_U8(
        dht_temperature
    );

    UART_Write(',');


    UART_Write_U8(
        dht_humidity
    );

    UART_Write(',');


    UART_Write(
        dht_valid
        ? '1'
        : '0'
    );


    UART_Write_String(
        "\r\n"
    );
}


// ============================================================
// BUZZER COMMAND ACK
// ============================================================

void UART_Send_Buzzer_ACK(
    uint8_t requested_state
)
{
    UART_Write_String(
        "ACK,B,"
    );


    UART_Write(
        requested_state
        ? '1'
        : '0'
    );

    UART_Write(',');


    UART_Write(
        buzzer_actual
        ? '1'
        : '0'
    );

    UART_Write(',');


    UART_Write(
        emergency_active
        ? '1'
        : '0'
    );


    UART_Write_String(
        "\r\n"
    );
}


// ============================================================
// ESP32 -> PIC COMMAND
// ============================================================

void Apply_Buzzer_Request(
    uint8_t requested_state
)
{
    venus_buzzer_request =
        requested_state
        ? 1U
        : 0U;

    Update_Safety_State();

    UART_Send_Buzzer_ACK(
        requested_state
    );

    UART_Send_State();
}


void Process_UART_Byte(
    char received_byte
)
{
    static uint8_t waiting_for_buzzer_value =
        0U;

    if (received_byte == 'B')
    {
        waiting_for_buzzer_value =
            1U;

        return;
    }

    if (!waiting_for_buzzer_value)
    {
        return;
    }

    waiting_for_buzzer_value =
        0U;

    if (received_byte == '1')
    {
        Apply_Buzzer_Request(
            1U
        );
    }

    else if (received_byte == '0')
    {
        Apply_Buzzer_Request(
            0U
        );
    }
}


// ============================================================
// MCU INITIALIZATION
// ============================================================

void MCU_Init(void)
{
    // Internal 8 MHz oscillator.

    OSCCON = 0x70;

    __delay_ms(10);


    // Disable comparators.

    CMCON = 0x07;


    // All analog-capable pins digital.

    ADCON1 = 0x0F;


    // MQ2

    TRISAbits.TRISA0 =
        1U;


    // KY026

    TRISBbits.TRISB1 =
        1U;


    // DHT11 idle = input

    DHT_TRIS =
        1U;


    // Buzzer

    BUZZER_TRIS =
        0U;


    Set_Buzzer_Output(
        0U
    );
}


// ============================================================
// MAIN
// ============================================================

void main(void)
{
    uint8_t previous_gas =
        0xFFU;

    uint8_t previous_flame =
        0xFFU;

    uint8_t previous_buzzer =
        0xFFU;


    /*
     * Main loop delay ≈ 10 ms.
     *
     * 100 ticks = ~1 second
     * 200 ticks = ~2 seconds
     */

    uint16_t telemetry_tick =
        0U;

    uint16_t dht_tick =
        0U;


    MCU_Init();


    UART_Init(
        9600
    );


    __delay_ms(500);


    UART_Write_String(
        "PIC,BOX1_READY\r\n"
    );


    while (1)
    {
        // ====================================================
        // FAST LOCAL SAFETY REFLEX
        // ====================================================

        Update_Safety_State();


        // ====================================================
        // DHT11 READ EVERY ~2 SECONDS
        // ====================================================

        dht_tick++;


        if (
            dht_tick >= 200U
        )
        {
            uint8_t new_temperature;
            uint8_t new_humidity;


            dht_tick = 0U;


            if (
                DHT11_Read(
                    &new_temperature,
                    &new_humidity
                )
            )
            {
                dht_temperature =
                    new_temperature;

                dht_humidity =
                    new_humidity;

                dht_valid =
                    1U;
            }

            else
            {
                /*
                 * Preserve last valid values,
                 * but report that current read failed.
                 */

                dht_valid =
                    0U;
            }


            /*
             * Re-check emergency sensors immediately
             * after the DHT timing transaction.
             */

            Update_Safety_State();
        }


        // ====================================================
        // IMMEDIATE SAFETY STATE CHANGE
        // ====================================================

        if (
            gas_triggered
            != previous_gas

            ||

            flame_triggered
            != previous_flame

            ||

            buzzer_actual
            != previous_buzzer
        )
        {
            previous_gas =
                gas_triggered;


            previous_flame =
                flame_triggered;


            previous_buzzer =
                buzzer_actual;


            UART_Send_State();
        }


        // ====================================================
        // PERIODIC TELEMETRY EVERY ~1 SECOND
        // ====================================================

        telemetry_tick++;


        if (
            telemetry_tick >= 100U
        )
        {
            telemetry_tick =
                0U;


            UART_Send_State();
        }


        // ====================================================
        // UART RX
        // ====================================================

       while (
            UART_Available()
        )
        {
            char received_byte =
                UART_Read();

            Process_UART_Byte(
                received_byte
            );
        }


        __delay_ms(10);
    }
}