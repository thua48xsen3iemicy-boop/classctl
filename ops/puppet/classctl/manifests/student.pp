class classctl::student (
  Pattern[/\Ahttps:\/\/[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?\z/] $coordinator,
  Pattern[/\A[A-Za-z0-9][A-Za-z0-9._-]{0,95}\z/] $node_id = $facts['networking']['hostname'],
  String[1] $repository = 'https://github.com/thua48xsen3iemicy-boop/classctl.git',
  String[1] $branch = 'main',
  Optional[String[1]] $public_source = undef,
) {
  classctl::installation { 'student':
    repository    => $repository,
    branch        => $branch,
    arguments     => [$node_id, $coordinator],
    public_source => $public_source,
  }
}
