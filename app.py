from kubernetes import client, config, watch
from kubernetes.client.exceptions import ApiException
from flask_sse import sse
from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, Response
from flask_cors import CORS
import subprocess
import os
import socket
import re
import requests
import json
import platform
import cpuinfo
import functools
import time

app = Flask(__name__)
CORS(app)  # This will enable CORS for all routes

app.secret_key = 'my_secret_key'
port_check_api = 'http://193.29.62.183:8081/check-port'

# Custom cache dictionary to store the cached results
cache = {}


def get_cached_result(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        key = (func.__name__, args, frozenset(kwargs.items()))

        # Check if the result is already cached and not expired
        if key in cache and time.time() - cache[key][1] <= 3600:
            return cache[key][0]

        # Invoke the function and store the result in the cache
        result = func(*args, **kwargs)
        cache[key] = (result, time.time())
        return result

    return wrapper


@get_cached_result
def get_balance(account_address):
    gpu_enabled = False
    variables_file_path = '/home/akash/variables'
    if os.path.exists(variables_file_path):
        with open(variables_file_path, 'r') as variables_file:
            variables = variables_file.read()
            if 'GPU_ENABLED=' in variables:
                gpu_enabled = True
    api_url = 'https://akash-api.polkachu.com:443' if not gpu_enabled else 'https://akash-api.polkachu.com:443'
    try:
        with requests.get(f'{api_url}/cosmos/bank/v1beta1/balances/{account_address}') as response:
            print(f"API Response: {response.text}")  # Debugging line
            # Check if the request was successful
            if response.status_code == 200:
                data = response.json()
                balances = data.get('balances', [])
                if len(balances) > 0:
                    for balance in balances:
                        amount = balance.get('amount')
                        if amount:
                            amount = int(amount)  # Convert amount to an integer
                            return amount / 1000000  # Divide amount by 1,000,000
            else:
                print(f"Failed to get balance. Status code: {response.status_code}")
    except requests.RequestException as e:
        print(f"An error occurred while fetching the balance: {e}")

    return 0  # Set balance to 0 if no balance is found or an error occurs

@get_cached_result
def get_location(public_ip):
    try:
        response = requests.get(f'http://ip-api.com/json/{public_ip}')
        data = response.json()
        return data['regionName']
    except:
        return "Unknown"


@get_cached_result
def get_public_ip():
    services = [
        'https://api.ipify.org',
        'https://icanhazip.com',
        'https://ident.me',
        'https://checkip.amazonaws.com'
    ]

    for service in services:
        try:
            response = requests.get(service)
            if response.status_code == 200:
                return response.text.strip()
        except requests.RequestException:
            pass
    return None


@get_cached_result
def get_local_ip():
    try:
        # Run the ifconfig command to get the network interfaces information
        output = subprocess.check_output(["ifconfig"]).decode("utf-8")

        # Use regular expressions to find the local IP address
        ip_regex = r"inet (?:addr:)?([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)"
        match = re.search(ip_regex, output)
        if match:
            return match.group(1)
        else:
            return None
    except subprocess.CalledProcessError:
        return None




def get_pod_status(api_instance, pod_name, namespace):
    try:
        pod_info = api_instance.read_namespaced_pod_status(pod_name, namespace)
        return pod_info.status.phase
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return 'Not Found'
        else:
            # Print the exception for debugging purposes
            print(f"Exception occurred while retrieving pod status: {str(e)}")
            return 'Error'
    except Exception as e:
        # Print the exception for debugging purposes
        print(f"Exception occurred while retrieving pod status: {str(e)}")
        return 'Error'

def check_service_status():
    try:
        config.load_kube_config()
        api_instance = client.CoreV1Api()
        namespace = 'akash-services'

        rpc_node_status = get_pod_status(api_instance, 'akash-node-1-0', namespace)
        provider_status = get_pod_status(api_instance, 'akash-provider-0', namespace)
        both_services_status = 'Online' if rpc_node_status == 'Running' and provider_status == 'Running' else 'Provider Offline'

        return rpc_node_status, provider_status, both_services_status
    except Exception as e:
        # Print the exception for debugging purposes
        print(f"Exception occurred while checking service status: {str(e)}")
        return 'Error', 'Error', 'Error'


@app.route('/', methods=['GET', 'POST'])
def dashboard():
    if request.method == 'POST':
        # Save the variables from the form
        new_variables = request.form.to_dict()

        # Read the existing variables
        existing_variables = {}
        with open('/home/akash/variables', 'r') as f:
            for line in f:
                key, value = line.strip().split('=')
                existing_variables[key] = value

        # Update the existing variables with the new ones
        existing_variables.update(new_variables)

        # Write the updated variables back to the file
        with open('/home/akash/variables', 'w') as f:
            for key, value in existing_variables.items():
                f.write(f'{key}={value}\n')

        # Update the values in bid-engine-script.sh
        bid_engine_script_path = '/home/akash/bid-engine-script.sh'
        with open(bid_engine_script_path, 'r') as f:
            script_lines = f.readlines()

        updated_script_lines = []
        for line in script_lines:
            if line.startswith('TARGET_MEMORY='):
                line = f'TARGET_MEMORY="{existing_variables.get("MEMORY_PRICE", "0.75")}"\n'
            elif line.startswith('TARGET_HD='):
                line = f'TARGET_HD="{existing_variables.get("DISK_PRICE", "0.25")}"\n'
            elif line.startswith('TARGET_CPU='):
                line = f'TARGET_CPU="{existing_variables.get("CPU_PRICE", "1.50")}"\n'
            updated_script_lines.append(line)

        with open(bid_engine_script_path, 'w') as f:
            f.writelines(updated_script_lines)

        flash('Variables saved successfully. Provider restart is required.', 'success')
        return redirect('/')
        
    else:
        # Read the variables file
        with open('/home/akash/variables', 'r') as f:
            variables = dict(line.strip().split('=') for line in f)

        # If 'ACCOUNT_ADDRESS' is not in variables, return or handle it properly
        if 'ACCOUNT_ADDRESS' not in variables:
            return "Account address not found in variables."

        account_address = variables['ACCOUNT_ADDRESS']
        balance = get_balance(account_address)

        # Read the DNS records file
        with open('/home/akash/dns-records.txt', 'r') as f:
            dns_records = f.read()

        # Read the firewall ports file
        with open('/home/akash/firewall-ports.txt', 'r') as f:
            firewall_ports = f.read()

        with open('/home/akash/wallet_qr_code.txt', 'r') as f:
            qr_code = f.read()

        local_ip = get_local_ip()
        public_ip = get_public_ip()
        info = cpuinfo.get_cpu_info()
        location = get_location(public_ip)

        if 'AMD' in info['brand_raw']:
            processor = 'AMD'
        elif 'Intel' in info['brand_raw']:
            processor = 'Intel'
        else:
            processor = 'Unknown'

    # Check service status
    rpc_node_status, provider_status, both_services_status = check_service_status()

    # Render the dashboard page with the variables, DNS records, firewall ports, and service status
    return render_template('dashboard.html',
                           variables=variables,
                           dns_records=dns_records,
                           firewall_ports=firewall_ports,
                           local_ip=local_ip,
                           public_ip=public_ip,
                           qr_code=qr_code,
                           processor=info['brand_raw'],
                           region=location,
                           balance=balance,
                           rpc_node_status=rpc_node_status,
                           provider_status=provider_status,
                           both_services_status=both_services_status)


@app.route('/deploy-update', methods=['GET'])
def deploy_update():
    script_path = '/home/akash/run-helm-k3s.sh'
    user = 'akash'  # Replace 'your_system_user' with the actual system username

    result = subprocess.run(['sudo', '-u', user, 'bash', script_path], shell=False)

    if result.returncode == 0:
        return 'Script executed successfully.'
    else:
        return 'Failed to execute script.'

@app.route('/ports', methods=['GET'])
def ports():
    # Check if ports are reachable and display the information
    ports_info = subprocess.check_output(["port-check-command"])
    return jsonify(ports_info=ports_info)


@app.route('/download_key', methods=['GET'])
def download_key():
    # Handle private key download
    # Use Akash CLI to export the private key
    # subprocess.run(["akash", "keys", "export", "default"])
    return send_file('/home/akash/key.pem', as_attachment=True)


@app.route('/download_variables', methods=['GET'])
def download_variables():
    # Handle private key download
    # Use Akash CLI to export the private key
    # subprocess.run(["akash", "keys", "export", "default"])
    return send_file('/home/akash/variables', as_attachment=True)


@app.route('/download_kubeconfig', methods=['GET'])
def download_kubeconfig():
    # Read the variables file
    with open('/home/akash/variables', 'r') as f:
        variables = dict(line.strip().split('=') for line in f)

    kubeconfig_path = variables.get('KUBECONFIG')

    if kubeconfig_path:
        return send_file(kubeconfig_path, as_attachment=True)
    else:
        return "Kubeconfig path not found in variables."





# Function to retrieve the Hostname Operator pod status
def get_hostname_operator_status():
    config.load_kube_config()
    api_instance = client.CoreV1Api()
    namespace = 'akash-services'
    try:
        # Retrieve the list of all pods in the specified namespace
        pod_list = api_instance.list_namespaced_pod(namespace)

        # Find the pod with 'corehostname_operator-' in its name
        hostname_operator_pod = next((pod for pod in pod_list.items if 'akash-hostname-operator-' in pod.metadata.name), None)

        if hostname_operator_pod is not None:
            # Retrieve the status of the 'coredns-' pod
            pod_info = api_instance.read_namespaced_pod_status(hostname_operator_pod.metadata.name, namespace)
            return pod_info.status.phase
        else:
            # If no pod with 'coredns-' in its name is found, return 'Not Found'
            return 'Not Found'

    except ApiException as e:
        # Handle Kubernetes API exceptions
        if e.status == 404:
            return 'Not Found'
        else:
            print(f"APIException occurred while retrieving hostname_operator pod status: {str(e)}")
            return 'Error'
    except Exception as e:
        # Handle any other exceptions that occur during retrieval
        print(f"Exception occurred while retrieving hostname_operator pod status: {str(e)}")
        return 'Error'
# Function to subscribe to hostname_operator status updates
def subscribe_to_hostname_operator_status():
    while True:
        # Retrieve the hostname_operator status
        hostname_operator_status = get_hostname_operator_status()

        # Yield the hostname_operator status as SSE data
        yield 'data: ' + json.dumps({'status': hostname_operator_status}) + '\n\n'

@app.route('/stream/hostname_operator_status', methods=['GET'])
def stream_hostname_operator_status():
    return Response(subscribe_to_hostname_operator_status(), mimetype='text/event-stream')



# Function to retrieve the DNS pod status
def get_dns_status():
    config.load_kube_config()
    api_instance = client.CoreV1Api()
    namespace = 'kube-system'
    try:
        # Retrieve the list of all pods in the specified namespace
        pod_list = api_instance.list_namespaced_pod(namespace)

        # Find the pod with 'coredns-' in its name
        coredns_pod = next((pod for pod in pod_list.items if 'coredns-' in pod.metadata.name), None)

        if coredns_pod is not None:
            # Retrieve the status of the 'coredns-' pod
            pod_info = api_instance.read_namespaced_pod_status(coredns_pod.metadata.name, namespace)
            return pod_info.status.phase
        else:
            # If no pod with 'coredns-' in its name is found, return 'Not Found'
            return 'Not Found'

    except ApiException as e:
        # Handle Kubernetes API exceptions
        if e.status == 404:
            return 'Not Found'
        else:
            print(f"APIException occurred while retrieving DNS pod status: {str(e)}")
            return 'Error'
    except Exception as e:
        # Handle any other exceptions that occur during retrieval
        print(f"Exception occurred while retrieving DNS pod status: {str(e)}")
        return 'Error'

# Function to subscribe to dns status updates
def subscribe_to_dns_status():
    while True:
        # Retrieve the dns status
        dns_status = get_dns_status()

        # Yield the dns status as SSE data
        yield 'data: ' + json.dumps({'status': dns_status}) + '\n\n'

@app.route('/stream/dns_status', methods=['GET'])
def stream_dns_status():
    return Response(subscribe_to_dns_status(), mimetype='text/event-stream')


# Function to retrieve the Provider pod status
def get_provider_status():
    config.load_kube_config()
    api_instance = client.CoreV1Api()
    namespace = 'akash-services'
    try:
        # Retrieve the provider pod status from Kubernetes
        pod_info = api_instance.read_namespaced_pod_status('akash-provider-0', namespace)
        return pod_info.status.phase
    except ApiException as e:
        # Handle Kubernetes API exceptions
        if e.status == 404:
            return 'Not Found'
        else:
            print(f"APIException occurred while retrieving provider pod status: {str(e)}")
            return 'Error'
    except Exception as e:
        # Handle any other exceptions that occur during retrieval
        print(f"Exception occurred while retrieving provider pod status: {str(e)}")
        return 'Error'

# Function to subscribe to provider status updates
def subscribe_to_provider_status():
    while True:
        # Retrieve the provider status
        provider_status = get_provider_status()

        # Yield the provider status as SSE data
        yield 'data: ' + json.dumps({'status': provider_status}) + '\n\n'

@app.route('/stream/provider_status', methods=['GET'])
def stream_provider_status():
    return Response(subscribe_to_provider_status(), mimetype='text/event-stream')




# Function to retrieve the RPC pod status
def get_rpc_status():
    config.load_kube_config()
    api_instance = client.CoreV1Api()
    namespace = 'akash-services'
    try:
        # Retrieve the RPC pod status from Kubernetes
        pod_info = api_instance.read_namespaced_pod_status('akash-node-1-0', namespace)
        return pod_info.status.phase
    except ApiException as e:
        # Handle Kubernetes API exceptions
        if e.status == 404:
            return 'Not Found'
        else:
            print(f"APIException occurred while retrieving RPC pod status: {str(e)}")
            return 'Error'
    except Exception as e:
        # Handle any other exceptions that occur during retrieval
        print(f"Exception occurred while retrieving RPC pod status: {str(e)}")
        return 'Error'

# Function to subscribe to RPC status updates
def subscribe_to_rpc_status():
    while True:
        # Retrieve the RPC status
        rpc_status = get_rpc_status()

        # Yield the RPC status as SSE data
        yield 'data: ' + json.dumps({'status': rpc_status}) + '\n\n'

@app.route('/stream/rpc_status', methods=['GET'])
def stream_rpc_status():
    return Response(subscribe_to_rpc_status(), mimetype='text/event-stream')



@app.route('/stop_service', methods=['POST'])
def stop_service():
    service_name = request.form.get('service_name')
    namespace = 'akash-services'

    if service_name == 'rpc':
        scale_down_stateful_set(namespace, 'akash-node-1')
    elif service_name == 'provider':
        scale_down_stateful_set(namespace, 'akash-provider')
    elif service_name == 'both':
        scale_down_stateful_set(namespace, 'akash-node-1')
        scale_down_stateful_set(namespace, 'akash-provider')

    return redirect('/')


@app.route('/start_service', methods=['POST'])
def start_service():
    service_name = request.form.get('service_name')
    namespace = 'akash-services'

    if service_name == 'rpc':
        scale_up_stateful_set(namespace, 'akash-node-1')
    elif service_name == 'provider':
        scale_up_stateful_set(namespace, 'akash-provider')
    elif service_name == 'both':
        scale_up_stateful_set(namespace, 'akash-node-1')
        scale_up_stateful_set(namespace, 'akash-provider')

    return redirect('/')


@app.route('/restart_service', methods=['POST'])
def restart_service():
    service_name = request.form.get('service_name')
    namespace = 'akash-services'

    if service_name == 'rpc':
        scale_down_stateful_set(namespace, 'akash-node-1')
        scale_up_stateful_set(namespace, 'akash-node-1')

    elif service_name == 'provider':
        scale_down_stateful_set(namespace, 'akash-provider')
        scale_up_stateful_set(namespace, 'akash-provder')

    elif service_name == 'both':
        scale_down_stateful_set(namespace, 'akash-node-1')
        scale_up_stateful_set(namespace, 'akash-node-1')
        scale_down_stateful_set(namespace, 'akash-provider')
        scale_up_stateful_set(namespace, 'akash-provder')


    return redirect('/')

@app.route('/get_service_status', methods=['POST'])
def get_service_status():

    config.load_kube_config()
    api_instance = client.CoreV1Api()
    namespace = 'akash-services'

    # Get status for RPC Node
    rpc_node_status = get_pod_status(api_instance, 'akash-node-1', namespace)

    # Get status for Provider
    provider_status = get_pod_status(api_instance, 'akash-provider', namespace)

    # Get status for both services
    both_services_status = 'Online' if rpc_node_status == 'Running' and provider_status == 'Running' else 'Offline'

    return jsonify({
        'rpc_node_status': rpc_node_status,
        'provider_status': provider_status,
        'both_services_status': both_services_status
    })



def scale_down_stateful_set(namespace, stateful_set_name):
    config.load_kube_config()
    api_instance = client.AppsV1Api()

    stateful_set = api_instance.read_namespaced_stateful_set(stateful_set_name, namespace)
    stateful_set.spec.replicas = 0
    api_instance.replace_namespaced_stateful_set(stateful_set_name, namespace, stateful_set)


def scale_up_stateful_set(namespace, stateful_set_name):
    config.load_kube_config()
    api_instance = client.AppsV1Api()

    stateful_set = api_instance.read_namespaced_stateful_set(stateful_set_name, namespace)
    stateful_set.spec.replicas = 1
    api_instance.replace_namespaced_stateful_set(stateful_set_name, namespace, stateful_set)


def restart_stateful_set(namespace, stateful_set_name):
    config.load_kube_config()
    api_instance = client.AppsV1Api()

    body = client.V1beta1RollbackConfig()
    body.rollback_to = client.V1beta1RollbackDeployment()
    body.rollback_to.revision = 0
    body.rollback_to.rollback_to = None

    api_instance.create_namespaced_stateful_set_rollback(stateful_set_name, namespace, body)


# Usage

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
