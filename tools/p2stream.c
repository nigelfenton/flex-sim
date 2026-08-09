/* p2stream.c — openHPSDR P2 discovery + run-bit + DDC0 IQ consumer.
 *
 * Send shapes and parse offsets transcribed from piHPSDR (GPL-3.0,
 * Laurence Barker) so this agrees with a REAL client, not with our simulator:
 *   - discovery: src/new_discovery.c   (60 B, [4]=0x02; reply [4] status,
 *                                       [11] device, [12] P2 ver, [20] DDCs)
 *   - high priority: src/new_protocol.c
 *       high_priority_buffer_to_radio[1444]   <- SIZE, not 60
 *       [0:4] sequence, [4] = running (the RUN bit), [9:13] DDC0 phase/freq
 *   - DDC IQ parse: src/new_protocol.c process_ddc_iq
 *       [0:4] seq, [4:12] timestamp, [12:14] bits/sample,
 *       [14:16] samplesperframe, samples from b=16, 24-bit BE I then Q
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>

#define DISCOVERY_PORT 1024
#define HP_TO_RADIO_PORT 1027

static int fails = 0;
static void check(const char *what, int ok, const char *detail) {
    if (ok) printf("  PASS  %s\n", what);
    else { printf("  FAIL  %s  %s\n", what, detail ? detail : ""); fails++; }
}

int main(int argc, char **argv) {
    const char *host = (argc > 1) ? argv[1] : "127.0.0.1";
    struct sockaddr_in to; memset(&to, 0, sizeof(to));
    to.sin_family = AF_INET; to.sin_addr.s_addr = inet_addr(host);

    int s = socket(AF_INET, SOCK_DGRAM, 0);
    struct timeval tv = {3, 0};
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    /* ---- 1. discovery ---------------------------------------------- */
    unsigned char buf[60]; memset(buf, 0, sizeof(buf)); buf[4] = 0x02;
    to.sin_port = htons(DISCOVERY_PORT);
    sendto(s, buf, 60, 0, (struct sockaddr*)&to, sizeof(to));
    unsigned char rx[4096];
    ssize_t n = recvfrom(s, rx, sizeof(rx), 0, NULL, NULL);
    if (n < 0) { printf("FAIL: no discovery reply\n"); return 1; }
    check("discovery reply accepted (status 2/3, device resolves)",
          rx[0]==0 && rx[4]==2 && (rx[11]&0xFF)==0x0A, "device != Saturn");
    printf("        device %d, %d DDC, P2 v%.1f\n",
           1000+(rx[11]&0xFF), rx[20]&0xFF, (rx[12]&0xFF)/10.0);

    /* ---- 2. no IQ before the run bit -------------------------------- */
    tv.tv_sec = 1; setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    n = recvfrom(s, rx, sizeof(rx), 0, NULL, NULL);
    check("silent before the run bit", n < 0, "got data unbidden");

    /* ---- 3. run bit = 1, piHPSDR's 1444-byte high-priority packet ---- */
    static unsigned char hp[1444];
    memset(hp, 0, sizeof(hp));
    hp[4] = 1;                                   /* = running */
    unsigned int f = 14100000;                   /* DDC0 phase/freq word */
    hp[9]=f>>24; hp[10]=f>>16; hp[11]=f>>8; hp[12]=f;
    to.sin_port = htons(HP_TO_RADIO_PORT);
    sendto(s, hp, sizeof(hp), 0, (struct sockaddr*)&to, sizeof(to));
    printf("  sent RUN=1 (1444 B high-priority, piHPSDR shape)\n");

    /* ---- 4. consume DDC0 IQ ----------------------------------------- */
    tv.tv_sec = 3; setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    int pkts = 0, badlen = 0, badseq = 0, nonzero = 0;
    long prev = -1, firstseq = -1;
    struct timeval t0, t1; gettimeofday(&t0, NULL);
    while (pkts < 200) {
        n = recvfrom(s, rx, sizeof(rx), 0, NULL, NULL);
        if (n < 0) break;
        if (n != 1456) { badlen++; continue; }   /* 16 header + 1440 payload */
        long seq = ((long)(rx[0]&0xFF)<<24)|((rx[1]&0xFF)<<16)|((rx[2]&0xFF)<<8)|(rx[3]&0xFF);
        if (firstseq < 0) firstseq = seq;
        if (prev >= 0 && seq != prev+1) badseq++;
        prev = seq;
        int bits = ((rx[12]&0xFF)<<8) + (rx[13]&0xFF);
        int spf  = ((rx[14]&0xFF)<<8) + (rx[15]&0xFF);
        if (pkts == 0) {
            check("bits/sample = 24 (piHPSDR offset [12:14])", bits == 24, "wrong");
            check("samplesperframe = 240 (piHPSDR offset [14:16])", spf == 240, "wrong");
        }
        /* decode sample 0 exactly as process_ddc_iq does */
        int b = 16;
        int I = ((int)((signed char)rx[b])<<16) | (((rx[b+1]&0xFF)<<8)&0xFF00) | (rx[b+2]&0xFF);
        if (I != 0) nonzero++;
        pkts++;
    }
    gettimeofday(&t1, NULL);
    double el = (t1.tv_sec-t0.tv_sec) + (t1.tv_usec-t0.tv_usec)/1e6;

    check("DDC0 IQ received after RUN=1", pkts >= 20, "too few packets");
    check("every packet is 1456 B (16+1440)", badlen == 0, "wrong-length packets seen");
    check("sequence numbers monotonic", badseq == 0, "gaps/repeats");
    check("payload decodes to non-zero samples", nonzero > pkts/2, "all zero");
    if (pkts > 1) {
        double per_ms = el / (pkts-1) * 1000.0;
        char d[64]; snprintf(d, sizeof(d), "measured %.2f ms", per_ms);
        check("cadence ~5 ms/packet at 48 kHz", per_ms > 2.0 && per_ms < 20.0, d);
        printf("        %d packets in %.2f s = %.2f ms/pkt\n", pkts, el, per_ms);
    }

    /* ---- 5. run bit = 0 stops it ------------------------------------ */
    hp[4] = 0;
    sendto(s, hp, sizeof(hp), 0, (struct sockaddr*)&to, sizeof(to));
    usleep(400000);
    tv.tv_sec = 0; tv.tv_usec = 300000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    while (recvfrom(s, rx, sizeof(rx), 0, NULL, NULL) > 0) { }   /* drain */
    tv.tv_sec = 1; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    n = recvfrom(s, rx, sizeof(rx), 0, NULL, NULL);
    check("RUN=0 stops the stream", n < 0, "still streaming");

    printf("\n%s\n", fails ? "FAILURES PRESENT" : "all checks passed");
    return fails ? 1 : 0;
}
