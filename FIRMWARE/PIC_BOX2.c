/*
VENUS CPS - BOX 2 PIC18F4520
Hardware:
RGB common cathode
Red   RD0, physical pin 19 (330 ohm)
Green RD1, physical pin 20 (330 ohm)
Blue  RD2, physical pin 21 (330 ohm)

Door servo
Signal RC2/CCP1, physical pin 17
External regulated 5 V supply; common ground with PIC

UART 9600 8N1
RC6/TX pin 25 -> ESP32 GPIO18 RX
RC7/RX pin 26 <- ESP32 GPIO17 TX

ESP32 -> PIC:
L0 = light off
L1 = warm white, approximately 2700 K
L2 = natural white, approximately 3500 K
L3 = daylight, approximately 5000 K
D0 = door closed, 0 degrees
D1 = door open, 90 degrees
*/

#pragma config OSC = INTIO67
#pragma config WDT = OFF
#pragma config LVP = OFF
#pragma config PBADEN = OFF
#pragma config MCLRE = ON

#include <xc.h>
#include <stdint.h>

#define _XTAL_FREQ 8000000UL

/* RGB outputs: common cathode, HIGH means on. */
#define RGB_RED_LAT       LATDbits.LATD0
#define RGB_GREEN_LAT     LATDbits.LATD1
#define RGB_BLUE_LAT      LATDbits.LATD2
#define RGB_RED_TRIS      TRISDbits.TRISD0
#define RGB_GREEN_TRIS    TRISDbits.TRISD1
#define RGB_BLUE_TRIS     TRISDbits.TRISD2

/* Servo output. */
#define SERVO_LAT         LATCbits.LATC2
#define SERVO_TRIS        TRISCbits.TRISC2

#define PWM_MAX                 64U
#define SERVO_FRAME_US       20000U
#define SERVO_CLOSED_US       1000U
#define SERVO_OPEN_US         1500U

typedef enum
{
    LIGHT_OFF = 0,
    LIGHT_WARM_WHITE = 1,
    LIGHT_NATURAL_WHITE = 2,
    LIGHT_DAYLIGHT = 3
} light_mode_t;

volatile uint8_t red_level = 0U;
volatile uint8_t green_level = 0U;
volatile uint8_t blue_level = 0U;
volatile uint8_t pwm_counter = 0U;

volatile uint16_t servo_pulse_us = SERVO_CLOSED_US;
volatile uint16_t servo_next_compare = 1000U;
volatile uint8_t servo_high_phase = 0U;

volatile light_mode_t light_mode_actual = LIGHT_OFF;
volatile uint8_t door_open_actual = 0U;
volatile uint8_t door_angle_actual = 0U;

static void CCP1_SetCompare(uint16_t value)
{
    CCPR1H = (uint8_t)(value >> 8);
    CCPR1L = (uint8_t)(value & 0xFFU);
}

void __interrupt() System_ISR(void)
{
    /* Servo edges receive priority over the decorative light PWM. */
    if (PIR1bits.CCP1IF)
    {
        PIR1bits.CCP1IF = 0U;

        if (!servo_high_phase)
        {
            /* Rising edge has just occurred. Schedule falling edge. */
            servo_high_phase = 1U;
            CCP1CON = 0x09U; /* Clear RC2 on next compare. */
            servo_next_compare =
                (uint16_t)(servo_next_compare + servo_pulse_us);
        }
        else
        {
            /* Falling edge has just occurred. Schedule next frame. */
            servo_high_phase = 0U;
            CCP1CON = 0x08U; /* Set RC2 on next compare. */
            servo_next_compare =
                (uint16_t)(
                    servo_next_compare
                    + (SERVO_FRAME_US - servo_pulse_us)
                );
        }

        CCP1_SetCompare(servo_next_compare);
    }

    if (PIR1bits.TMR2IF)
    {
        PIR1bits.TMR2IF = 0U;

        RGB_RED_LAT = (pwm_counter < red_level) ? 1U : 0U;
        RGB_GREEN_LAT = (pwm_counter < green_level) ? 1U : 0U;
        RGB_BLUE_LAT = (pwm_counter < blue_level) ? 1U : 0U;

        pwm_counter++;

        if (pwm_counter >= PWM_MAX)
        {
            pwm_counter = 0U;
        }
    }
}

void UART_Init(uint32_t baudrate)
{
    uint16_t spbrg_value =
        (uint16_t)((_XTAL_FREQ / (4UL * baudrate)) - 1UL);

    TRISCbits.TRISC6 = 0U;
    TRISCbits.TRISC7 = 1U;

    TXSTA = 0x24U;
    RCSTA = 0x90U;
    BAUDCONbits.BRG16 = 1U;

    SPBRG = (uint8_t)(spbrg_value & 0xFFU);
    SPBRGH = (uint8_t)((spbrg_value >> 8) & 0xFFU);
}

void UART_Write(char data)
{
    while (!PIR1bits.TXIF)
    {
    }

    TXREG = data;
}

void UART_Write_String(const char *text)
{
    while (*text)
    {
        UART_Write(*text++);
    }
}

void UART_Write_U8(uint8_t value)
{
    if (value >= 100U)
    {
        UART_Write((char)('0' + (value / 100U)));
        value %= 100U;
        UART_Write((char)('0' + (value / 10U)));
        UART_Write((char)('0' + (value % 10U)));
    }
    else if (value >= 10U)
    {
        UART_Write((char)('0' + (value / 10U)));
        UART_Write((char)('0' + (value % 10U)));
    }
    else
    {
        UART_Write((char)('0' + value));
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

static void RGB_Set(uint8_t red, uint8_t green, uint8_t blue)
{
    uint8_t interrupts_were_enabled = INTCONbits.GIE;

    INTCONbits.GIE = 0U;
    red_level = red;
    green_level = green;
    blue_level = blue;
    INTCONbits.GIE = interrupts_were_enabled;
}

static void Light_SetMode(light_mode_t mode)
{
    switch (mode)
    {
        case LIGHT_WARM_WHITE:
            /* Calibrated equivalent of RGB(255,169,87). */
            RGB_Set(64U, 42U, 22U);
            break;

        case LIGHT_NATURAL_WHITE:
            /* Calibrated equivalent of RGB(255,196,137). */
            RGB_Set(64U, 49U, 34U);
            break;

        case LIGHT_DAYLIGHT:
            /* Calibrated equivalent of RGB(255,228,206). */
            RGB_Set(64U, 57U, 52U);
            break;

        case LIGHT_OFF:
        default:
            mode = LIGHT_OFF;
            RGB_Set(0U, 0U, 0U);
            break;
    }

    light_mode_actual = mode;
}

static void Door_SetOpen(uint8_t open)
{
    uint8_t interrupts_were_enabled = INTCONbits.GIE;

    INTCONbits.GIE = 0U;

    if (open)
    {
        servo_pulse_us = SERVO_OPEN_US;
        door_open_actual = 1U;
        door_angle_actual = 90U;
    }
    else
    {
        servo_pulse_us = SERVO_CLOSED_US;
        door_open_actual = 0U;
        door_angle_actual = 0U;
    }

    INTCONbits.GIE = interrupts_were_enabled;
}

void UART_Send_State(void)
{
    UART_Write_String("STATE,");
    UART_Write_U8((uint8_t)light_mode_actual);
    UART_Write(',');
    UART_Write(door_open_actual ? '1' : '0');
    UART_Write(',');
    UART_Write_U8(door_angle_actual);
    UART_Write_String("\r\n");
}

void UART_Send_Light_ACK(light_mode_t requested_mode)
{
    UART_Write_String("ACK,L,");
    UART_Write_U8((uint8_t)requested_mode);
    UART_Write(',');
    UART_Write_U8((uint8_t)light_mode_actual);
    UART_Write_String("\r\n");
}

void UART_Send_Door_ACK(uint8_t requested_open)
{
    UART_Write_String("ACK,D,");
    UART_Write(requested_open ? '1' : '0');
    UART_Write(',');
    UART_Write(door_open_actual ? '1' : '0');
    UART_Write(',');
    UART_Write_U8(door_angle_actual);
    UART_Write_String("\r\n");
}

static void Apply_Light_Request(light_mode_t requested_mode)
{
    Light_SetMode(requested_mode);
    UART_Send_Light_ACK(requested_mode);
    UART_Send_State();
}

static void Apply_Door_Request(uint8_t requested_open)
{
    Door_SetOpen(requested_open);
    UART_Send_Door_ACK(requested_open);
    UART_Send_State();
}

void Process_UART_Byte(char received_byte)
{
    static char pending_command = 0;

    if (received_byte == 'L' || received_byte == 'D')
    {
        pending_command = received_byte;
        return;
    }

    if (pending_command == 'L')
    {
        pending_command = 0;

        if (received_byte >= '0' && received_byte <= '3')
        {
            Apply_Light_Request(
                (light_mode_t)(received_byte - '0')
            );
        }

        return;
    }

    if (pending_command == 'D')
    {
        pending_command = 0;

        if (received_byte == '0' || received_byte == '1')
        {
            Apply_Door_Request(
                (received_byte == '1') ? 1U : 0U
            );
        }
    }
}

static void RGB_PWM_Init(void)
{
    RGB_RED_LAT = 0U;
    RGB_GREEN_LAT = 0U;
    RGB_BLUE_LAT = 0U;

    RGB_RED_TRIS = 0U;
    RGB_GREEN_TRIS = 0U;
    RGB_BLUE_TRIS = 0U;

    /*
     * Timer2 interrupt = 15.625 kHz.
     * 64 PWM steps produce approximately 244 Hz RGB PWM.
     */
    T2CON = 0x01U; /* Prescaler 1:4, Timer2 initially off. */
    PR2 = 31U;
    TMR2 = 0U;
    PIR1bits.TMR2IF = 0U;
    PIE1bits.TMR2IE = 1U;
    T2CONbits.TMR2ON = 1U;
}

static void Servo_Init(void)
{
    SERVO_LAT = 0U;
    SERVO_TRIS = 0U;

    servo_pulse_us = SERVO_CLOSED_US;
    door_open_actual = 0U;
    door_angle_actual = 0U;
    servo_high_phase = 0U;

    /* Timer1 uses a 1 us tick: Fosc/4 with 1:2 prescaler. */
    T1CON = 0x90U;
    TMR1H = 0U;
    TMR1L = 0U;

    servo_next_compare = 1000U;
    CCP1_SetCompare(servo_next_compare);
    CCP1CON = 0x08U; /* Set RC2 on compare. */

    PIR1bits.CCP1IF = 0U;
    PIE1bits.CCP1IE = 1U;
    T1CONbits.TMR1ON = 1U;
}

void MCU_Init(void)
{
    OSCCON = 0x70U;
    __delay_ms(10);

    CMCON = 0x07U;
    ADCON1 = 0x0FU;

    Light_SetMode(LIGHT_OFF);
    RGB_PWM_Init();
    Servo_Init();

    RCONbits.IPEN = 0U;
    INTCONbits.PEIE = 1U;
    INTCONbits.GIE = 1U;
}

void main(void)
{
    uint16_t telemetry_tick = 0U;

    MCU_Init();
    UART_Init(9600UL);

    __delay_ms(500);
    UART_Write_String("PIC,BOX2_READY\r\n");
    UART_Send_State();

    while (1)
    {
        while (UART_Available())
        {
            Process_UART_Byte(UART_Read());
        }

        telemetry_tick++;

        if (telemetry_tick >= 100U)
        {
            telemetry_tick = 0U;
            UART_Send_State();
        }

        __delay_ms(10);
    }
}
