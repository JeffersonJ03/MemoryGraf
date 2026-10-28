locals {
  name = "app-${var.region}"
}

module "vpc" {
  source = "./modules/vpc"
  cidr   = "10.0.0.0/16"
}

module "eks" {
  source = "terraform-aws-modules/eks/aws"
}

resource "aws_security_group" "sg" {
  name   = local.name
  vpc_id = module.vpc.vpc_id
}

resource "aws_instance" "web" {
  subnet_id  = module.vpc.subnet_ids[0]
  depends_on = [aws_security_group.sg]
}
