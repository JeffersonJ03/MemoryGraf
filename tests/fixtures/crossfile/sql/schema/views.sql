CREATE OR REPLACE VIEW v_ordenes_abiertas AS
  SELECT o.id, c.nombre
  FROM ordenes o
  JOIN clientes c ON c.id = o.cliente_id
  WHERE o.estado <> 'FIN';
