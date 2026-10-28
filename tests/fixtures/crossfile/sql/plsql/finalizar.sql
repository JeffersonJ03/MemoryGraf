CREATE OR REPLACE PROCEDURE finalizar_orden(p_id IN NUMBER, p_out OUT VARCHAR2) AS
  v_estado VARCHAR2(20);
BEGIN
  SELECT estado INTO v_estado FROM ordenes WHERE id = p_id;
  UPDATE ordenes SET estado = 'FIN' WHERE id = p_id;
  pkg_util.log_evento(p_id);
  p_out := 'OK';
END finalizar_orden;
/
