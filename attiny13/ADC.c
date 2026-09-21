#include <avr/io.h>
#include <avr/interrupt.h>

/*
 * ATtiny13A
 *
 * PB0 / pin 5 = SS
 * PB1 / pin 6 = MISO
 * PB2 / pin 7 = SCK
 *
 * ADC3 / PB3 / pin 2 = MQ2
 * ADC2 / PB4 / pin 3 = MQ9
 *
 * Protocol:
 *
 * Pi pulls SS LOW
 *
 * ATtiny sends:
 *   byte 0 = MQ2 high byte
 *   byte 1 = MQ2 low byte
 *   byte 2 = MQ9 high byte
 *   byte 3 = MQ9 low byte
 *
 * SPI mode 0:
 *   CPOL = 0
 *   CPHA = 0
 *
 * Data changes on falling edge.
 * Pi samples on rising edge.
 */

volatile uint8_t txBuffer[4] = {0, 0, 0, 0};

volatile uint8_t byteCount = 0;
volatile uint8_t bitMask = 0x80;
volatile uint8_t currentByte = 0;
volatile uint8_t ssActive = 0;

volatile uint8_t previousPinB = 0;


/* ---------------------------------------------------------
 * ADC
 * --------------------------------------------------------- */

uint16_t readADC(uint8_t channel)
{
    /*
     * ADC3 = PB3 = physical pin 2
     * ADC2 = PB4 = physical pin 3
     */

    ADMUX = channel & 0x03;

    ADCSRA |= (1 << ADSC);

    while (ADCSRA & (1 << ADSC))
        ;

    return ADC;
}


/* ---------------------------------------------------------
 * Update sensor values
 * --------------------------------------------------------- */

void updateValues(void)
{
    uint16_t mq2 = readADC(3);
    uint16_t mq9 = readADC(2);

    /*
     * Interrupts are disabled while replacing
     * the 4-byte buffer so the SPI ISR cannot
     * observe a partially updated buffer.
     */

    cli();

    txBuffer[0] = (uint8_t)(mq2 >> 8);
    txBuffer[1] = (uint8_t)(mq2 & 0xFF);

    txBuffer[2] = (uint8_t)(mq9 >> 8);
    txBuffer[3] = (uint8_t)(mq9 & 0xFF);

    sei();
}


/* ---------------------------------------------------------
 * Start SPI transmission
 * --------------------------------------------------------- */

void startTransmission(void)
{
    ssActive = 1;

    byteCount = 0;
    bitMask = 0x80;

    currentByte = txBuffer[0];

    /*
     * First bit must already be present before
     * the Pi generates the first rising clock edge.
     */

    if (currentByte & bitMask)
        PORTB |= (1 << PB1);
    else
        PORTB &= ~(1 << PB1);

    /*
     * MISO output
     */
    DDRB |= (1 << PB1);
}


/* ---------------------------------------------------------
 * Stop SPI transmission
 * --------------------------------------------------------- */

void stopTransmission(void)
{
    ssActive = 0;

    /*
     * Release MISO.
     */
    DDRB &= ~(1 << PB1);
    PORTB &= ~(1 << PB1);
}


/* ---------------------------------------------------------
 * Pin Change Interrupt
 * --------------------------------------------------------- */

ISR(PCINT0_vect)
{
    uint8_t currentPinB = PINB;

    /*
     * -----------------------------------------------------
     * Detect SS transitions
     * -----------------------------------------------------
     */

    uint8_t previousSS = previousPinB & (1 << PB0);
    uint8_t currentSS  = currentPinB  & (1 << PB0);

    /*
     * SS HIGH -> LOW
     *
     * Start transaction.
     */
    if (previousSS && !currentSS)
    {
        startTransmission();
    }

    /*
     * SS LOW -> HIGH
     *
     * End transaction.
     */
    else if (!previousSS && currentSS)
    {
        stopTransmission();
    }

    /*
     * -----------------------------------------------------
     * Detect SCK falling edge
     * -----------------------------------------------------
     *
     * SPI mode 0:
     *
     * rising edge  = Pi samples data
     * falling edge = ATtiny changes data
     */

    uint8_t previousSCK = previousPinB & (1 << PB2);
    uint8_t currentSCK  = currentPinB  & (1 << PB2);

    /*
     * Falling edge:
     *
     * previous = HIGH
     * current  = LOW
     */
    if (ssActive && previousSCK && !currentSCK)
    {
        bitMask >>= 1;

        if (bitMask == 0)
        {
            bitMask = 0x80;

            byteCount++;

            if (byteCount < 4)
            {
                currentByte = txBuffer[byteCount];
            }
            else
            {
                currentByte = 0;
            }
        }

        /*
         * Put next bit on MISO.
         */
        if (currentByte & bitMask)
            PORTB |= (1 << PB1);
        else
            PORTB &= ~(1 << PB1);
    }

    previousPinB = currentPinB;
}


/* ---------------------------------------------------------
 * Main
 * --------------------------------------------------------- */

int main(void)
{
    /*
     * PB0 = SS input
     * PB1 = MISO initially input / high-Z
     * PB2 = SCK input
     */

    DDRB &= ~(1 << PB0);
    DDRB &= ~(1 << PB1);
    DDRB &= ~(1 << PB2);

    /*
     * SS pull-up
     */
    PORTB |= (1 << PB0);

    /*
     * SCK pull-down.
     *
     * This keeps the clock LOW when Pi GPIO is idle.
     */
    PORTB &= ~(1 << PB2);

    /*
     * ADC enabled.
     *
     * Assuming 9.6 MHz system clock:
     *
     * ADC clock = 9.6 MHz / 8 = 1.2 MHz
     */
    ADCSRA =
        (1 << ADEN) |
        (1 << ADPS1) |
        (1 << ADPS0);

    /*
     * Initial ADC readings.
     */
    updateValues();

    /*
     * Save initial pin state.
     */
    previousPinB = PINB;

    /*
     * Enable Pin Change Interrupt.
     */
    GIMSK |= (1 << PCIE);

    /*
     * Monitor:
     *
     * PB0 = SS
     * PB2 = SCK
     */
    PCMSK |=
        (1 << PCINT0) |
        (1 << PCINT2);

    sei();

    while (1)
    {
        /*
         * Update ADC values when the previous
         * SPI transaction has finished.
         *
         * We don't need a flag here if we update
         * periodically, but keeping the update
         * outside the ISR is important because ADC
         * conversion is slow.
         */

        if (!ssActive)
        {
            updateValues();
        }
    }

    return 0;
}