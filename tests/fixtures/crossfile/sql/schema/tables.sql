-- Tablas base (dialecto neutro)
CREATE TABLE clientes (
  id NUMBER PRIMARY KEY,
  nombre VARCHAR2(100)
);

CREATE TABLE ordenes (
  id NUMBER PRIMARY KEY,
  cliente_id NUMBER REFERENCES clientes(id),
  estado VARCHAR2(20)
);

CREATE TABLE eventos (orden_id NUMBER, creado DATE);
CREATE SEQUENCE seq_ordenes START WITH 1;
