class classctl::teacher (
  String[1] $repository = 'https://github.com/thua48xsen3iemicy-boop/classctl.git',
  String[1] $branch = 'main',
  Integer[1, 65535] $port = 8767,
  Integer[1, 65535] $ui_port = 8768,
  Optional[String[1]] $public_source = undef,
) {
  if $port == $ui_port {
    fail('classctl requires separate agent and UI ports')
  }
  classctl::installation { 'server':
    repository    => $repository,
    branch        => $branch,
    arguments     => [String($port), String($ui_port)],
    public_source => $public_source,
  }
}
