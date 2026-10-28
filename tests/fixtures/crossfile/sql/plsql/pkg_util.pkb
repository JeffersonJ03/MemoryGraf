CREATE OR REPLACE PACKAGE BODY pkg_util AS

  PROCEDURE log_evento(p_id NUMBER) IS
  BEGIN
    INSERT INTO eventos (orden_id, creado) VALUES (p_id, SYSDATE);
  END log_evento;

  FUNCTION siguiente_id RETURN NUMBER IS
  BEGIN
    RETURN seq_ordenes.NEXTVAL;
  END siguiente_id;

END pkg_util;
/
