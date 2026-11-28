define classctl::installation (
  String[1] $repository,
  String[1] $branch,
  Array[String[1], 2, 2] $arguments,
  Optional[String[1]] $public_source = undef,
) {
  $identity = $title ? { 'server' => 'server', 'student' => 'node' }
  $service_name = "classctl-${identity}"
  $installer = '/usr/local/sbin/classctl-puppet'
  file { $installer:
    ensure => file,
    owner  => 'root',
    group  => 'root',
    mode   => '0755',
    source => 'puppet:///modules/classctl/update.py',
  }

  if $public_source {
    ['room.json', 'policy.json'].each |$input| {
      file { "/etc/classctl/${input}":
        ensure => file,
        owner  => 'root',
        mode   => '0644',
        source => "${public_source}/${input}",
        before => Exec['apply classctl release'],
      }
    }
  }
  $params = [$repository, $branch, $title] + $arguments
  exec { 'apply classctl release':
    command => [$installer, 'apply'] + $params,
    unless  => [[$installer, 'current'] + $params],
    path    => ['/usr/local/sbin', '/usr/local/bin', '/usr/sbin', '/usr/bin', '/sbin', '/bin'],
    timeout => 1800,
    require => File[$installer],
  }
  service { $service_name:
    ensure  => running,
    enable  => true,
    require => Exec['apply classctl release'],
  }
}
