// Paani Kahan access policies (Cedar).
// Evaluated on every API request by app/policy.py. Edit and restart; no code changes.

// Anyone can check a route. Flood depth on public streets is public safety information.
@id("public-route-check")
permit (
  principal,
  action == Action::"checkRoute",
  resource == Layer::"routes"
);

// A saved commute reveals where someone lives and works. Only its owner can read or change it.
@id("own-commute-only")
permit (
  principal,
  action in [Action::"viewCommute", Action::"editCommute"],
  resource is Commute
)
when { resource.owner == principal };

// Anyone can report water (or a dry road) where they are. Reports are anonymous.
@id("anyone-reports-water")
permit (
  principal,
  action == Action::"submitReport",
  resource == Layer::"reports"
);

// Ward officers see pump and crew priorities, but only for the locality they run.
@id("officer-own-locality")
permit (
  principal in Role::"ward_officer",
  action == Action::"viewOps",
  resource is Locality
)
when { principal.locality == resource.name };

// Emergency control sees operations for every locality.
@id("emergency-all-localities")
permit (
  principal in Role::"emergency",
  action == Action::"viewOps",
  resource is Locality
);

// Which hospitals and schools are about to be cut off is restricted to emergency
// control, so it is not used to target or panic.
@id("emergency-vulnerable-sites")
permit (
  principal in Role::"emergency",
  action == Action::"viewVulnerableSites",
  resource == Layer::"vulnerable_sites"
);

// Hard stop: a suspended account can do nothing, whatever else is permitted.
@id("suspended-accounts")
forbid (
  principal,
  action,
  resource
)
when { principal has suspended && principal.suspended };
