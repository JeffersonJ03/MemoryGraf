package App::Billing;
use strict;
use App::Util qw(check);
use App::Model;
use List::Util qw(sum);
sub run { my $x = shift; check($x); App::Model::save($x); App::Model->load($x); helper(); }
sub helper { 1 }
1;
