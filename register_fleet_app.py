"""
One-time Fleet API partner registration for a Tesla developer app.

Tesla requires every Fleet API app to be registered in each region before
making user API calls.  This script:
  1. Gets a machine-to-machine (partner) auth token using client_credentials.
  2. Verifies the public key is reachable at the expected well-known URL.
  3. Calls POST /api/1/partner_accounts to register the domain.

Run once per region after creating your app on developer.tesla.com and hosting
your app's public key at:
  https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem

Usage:
  python3 register_fleet_app.py --client-id CLIENT_ID --client-secret SECRET --domain yourdomain.com
  python3 register_fleet_app.py --client-id CLIENT_ID --client-secret SECRET --domain yourdomain.com --region eu
"""

import argparse
import sys

import requests

FLEET_API_HOSTS = {
    'na': 'https://fleet-api.prd.na.vn.cloud.tesla.com',
    'eu': 'https://fleet-api.prd.eu.vn.cloud.tesla.com',
    'cn': 'https://fleet-api.prd.cn.vn.cloud.tesla.cn',
}
TOKEN_URL = 'https://auth.tesla.com/oauth2/v3/token'


def get_partner_token(client_id, client_secret, audience):
    """Exchange client credentials for a machine-to-machine partner token."""
    resp = requests.post(TOKEN_URL, data={
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': 'openid offline_access energy_device_data',
        'audience': audience,
    })
    resp.raise_for_status()
    return resp.json()['access_token']


def verify_public_key(domain):
    """Confirm the public key is reachable and looks like a PEM file."""
    url = f'https://{domain}/.well-known/appspecific/com.tesla.3p.public-key.pem'
    print(f'Checking public key at {url} ...')
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f'  ERROR: Could not fetch public key: {exc}', file=sys.stderr)
        print('  Make sure the file is hosted and publicly accessible before registering.', file=sys.stderr)
        sys.exit(1)
    if '-----BEGIN PUBLIC KEY-----' not in resp.text:
        print('  ERROR: Response does not look like a PEM public key.', file=sys.stderr)
        print(f'  Got: {resp.text[:200]}', file=sys.stderr)
        sys.exit(1)
    print('  Public key OK.')


def register(client_id, client_secret, domain, region):
    fleet_host = FLEET_API_HOSTS[region]
    print(f'Region:  {region}  ({fleet_host})')
    print(f'Domain:  {domain}')
    print()

    verify_public_key(domain)

    print('Getting partner auth token ...')
    token = get_partner_token(client_id, client_secret, fleet_host)
    print('  Token OK.')

    print('Registering domain with Fleet API ...')
    resp = requests.post(
        f'{fleet_host}/api/1/partner_accounts',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'domain': domain},
    )
    if resp.status_code == 200:
        print('  Registration successful!')
        print(f'  Response: {resp.json()}')
    else:
        print(f'  ERROR {resp.status_code}: {resp.text}', file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description='One-time Tesla Fleet API partner registration'
    )
    parser.add_argument('--client-id', required=True, help='Fleet API client ID')
    parser.add_argument('--client-secret', required=True, help='Fleet API client secret')
    parser.add_argument(
        '--domain',
        required=True,
        help='Domain hosting your public key (e.g. yourusername.github.io)',
    )
    parser.add_argument(
        '--region',
        default='na',
        choices=['na', 'eu', 'cn'],
        help='Fleet API region (default: na)',
    )
    args = parser.parse_args()
    register(args.client_id, args.client_secret, args.domain, args.region)


if __name__ == '__main__':
    main()
