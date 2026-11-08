000100 IDENTIFICATION DIVISION.                                         BILLING
000200 PROGRAM-ID. BILLING.                                             BILLING
000300 DATA DIVISION.                                                   BILLING
000400 WORKING-STORAGE SECTION.                                         BILLING
000500     COPY EMPREC.                                                 BILLING
000600 01  WS-PGM        PIC X(8) VALUE 'UTILPGM'.                      BILLING
000700     EXEC SQL INCLUDE SQLCA END-EXEC.                             BILLING
000800 PROCEDURE DIVISION.                                              BILLING
000900*    LA LÓGICA PRINCIPAL                                          BILLING
001000 MAIN-PARA.                                                       BILLING
001100     PERFORM CALC-PARA THRU CALC-EXIT                             BILLING
001200     CALL 'UTILPGM' USING WS-PGM                                  BILLING
001300     CALL WS-PGM                                                  BILLING
001400     EXEC SQL                                                     BILLING
001500         SELECT BALANCE INTO :WS-BAL FROM ACCOUNTS                BILLING
001600     END-EXEC                                                     BILLING
001700     EXEC CICS LINK PROGRAM('ACCTUPD') END-EXEC                   BILLING
001800     STOP RUN.                                                    BILLING
001900 CALC-PARA.                                                       BILLING
002000     DISPLAY 'CALC'.                                              BILLING
002100 CALC-EXIT.                                                       BILLING
002200     EXIT.                                                        BILLING
