# §28.1: the console in the home cluster calls satellite workers by pod IP (smoke, metrics, reload, canary). The VPC
# CNI gives pods VPC addresses, so a cross-region peering with routes both ways makes them reachable; the cluster
# security group (what managed-node-group pods' ENIs use) lets the home VPC in on 8080.
provider "aws" {
  alias  = "home"
  region = var.home_region
}

resource "aws_vpc_peering_connection" "home" {
  vpc_id      = aws_vpc.satellite.id
  peer_vpc_id = var.home_vpc_id
  peer_region = var.home_region
  tags        = merge(local.tags, { Name = "${local.name}-home" })
}

resource "aws_vpc_peering_connection_accepter" "home" {
  provider                  = aws.home
  vpc_peering_connection_id = aws_vpc_peering_connection.home.id
  auto_accept               = true
  tags                      = merge(local.tags, { Name = "${local.name}-home" })
}

resource "aws_route" "to_home" {
  route_table_id            = aws_route_table.public.id
  destination_cidr_block    = var.home_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.home.id
  depends_on                = [aws_vpc_peering_connection_accepter.home]
}

data "aws_route_tables" "home" {
  provider = aws.home
  vpc_id   = var.home_vpc_id
}

resource "aws_route" "from_home" {
  provider                  = aws.home
  for_each                  = length(var.home_route_table_ids) > 0 ? toset(var.home_route_table_ids) : toset(data.aws_route_tables.home.ids)
  route_table_id            = each.value
  destination_cidr_block    = var.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.home.id
  depends_on                = [aws_vpc_peering_connection_accepter.home]
}

resource "aws_vpc_security_group_ingress_rule" "home_to_workers" {
  security_group_id = aws_eks_cluster.satellite.vpc_config[0].cluster_security_group_id
  cidr_ipv4         = var.home_vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 8080
  to_port           = 8080
  description       = "ramen console (home VPC) to satellite worker pods"
  tags              = local.tags
}
