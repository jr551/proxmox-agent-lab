/* Compiled cert-pin check for the memflow RAM-injection PoC (authorized lab use).
 *
 * check() models a certificate pin compiled into a program: it accepts only
 * when the value matches a pinned constant, and rejects anything else (e.g. a
 * man-in-the-middle's certificate). The unique 64-bit constant gives its
 * `movabs` a findable signature in RAM, so `memflow scan` can locate check()
 * and `memflow phys-write` can NOP the reject branch -- overriding the pin in a
 * running process, with no restart.
 *
 *   gcc -O0 -fno-pic -no-pie -fcf-protection=none -o pincheck pincheck.c
 *   objdump --disassemble=check -M intel pincheck   # find movabs + jne offset
 *   ./pincheck                                        # prints REJECT pin-mismatch
 *
 * Then, from the controller (see README.md):
 *   scan for 48b88877665544332211 (the movabs of the pin constant),
 *   phys-write 9090 at <hit + 0x16> (the jne reject branch) -> ACCEPT.
 */
#include <stdio.h>
#include <unistd.h>

long check(long v)
{
    long pin = 0x1122334455667788L;
    if (v == pin)
        return 1;   /* pinned value matches -> accept */
    return 0;       /* anything else -> reject */
}

int main(void)
{
    for (;;) {
        int ok = check(0);   /* runtime value never matches the pin */
        puts(ok ? "ACCEPT pin-matched" : "REJECT pin-mismatch");
        fflush(stdout);
        sleep(2);
    }
}
