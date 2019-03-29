from kubernetes import client, config, watch
import os
import sys
import requests
from kubernetes.client.rest import ApiException
from urllib3.exceptions import ProtocolError
from requests.packages.urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import queue
import threading


def writeTextToFile(folder, filename, data):
    if not os.path.exists(folder):
        try:
            os.makedirs(folder)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise

    with open(folder +"/"+ filename, 'w') as f:
        f.write(data)
        f.close()


def request(url, method, payload = None):
    retryTotal = 5 if os.getenv('REQ_RETRY_TOTAL') is None else int(os.getenv('REQ_RETRY_TOTAL'))
    retryConnect = 5 if os.getenv('REQ_RETRY_CONNECT') is None else int(os.getenv('REQ_RETRY_CONNECT'))
    retryRead = 5 if os.getenv('REQ_RETRY_READ') is None else int(os.getenv('REQ_RETRY_READ'))
    retryBackoffFactor = 0.2 if os.getenv('REQ_RETRY_BACKOFF_FACTOR') is None else float(os.getenv('REQ_RETRY_BACKOFF_FACTOR'))
    timeout = 10 if os.getenv('REQ_TIMEOUT') is None else float(os.getenv('REQ_TIMEOUT'))

    r = requests.Session()
    retries = Retry(total = retryTotal,
            connect = retryConnect,
            read = retryRead,
            backoff_factor = retryBackoffFactor,
            status_forcelist = [ 500, 502, 503, 504 ])
    r.mount('http://', HTTPAdapter(max_retries=retries))
    r.mount('https://', HTTPAdapter(max_retries=retries))
    if url is None:
        print("No url provided. Doing nothing.")
        # If method is not provided use GET as default
    elif method == "GET" or method is None:
        res = r.get("%s" % url, timeout=timeout)
        print ("%s request sent to %s. Response: %d %s" % (method, url, res.status_code, res.reason))
    elif method == "POST":
        res = r.post("%s" % url, json=payload, timeout=timeout)
        print ("%s request sent to %s. Response: %d %s" % (method, url, res.status_code, res.reason))
    return res

def removeFile(folder, filename):
    completeFile = folder +"/"+filename
    if os.path.isfile(completeFile):
        os.remove(completeFile)
    else:
        print("Error: %s file not found" % completeFile)


def listConfigmaps(label, targetFolder, url, method, payload, current, folderAnnotation):
    v1 = client.CoreV1Api()
    namespace = os.getenv("NAMESPACE")
    namespace_label_selector = os.getenv("NAMESPACE_LABELS")
    if namespace is None and namespace_label_selector is None:
        ret = v1.list_namespaced_config_map(namespace=current)
        items = ret.items
    elif namespace == "ALL":
        ret = v1.list_config_map_for_all_namespaces()
        items = ret.items
    elif namespace_label_selector:
        match_labels = dict(m.split("=") for m in namespace_label_selector.split(","))
        items = []
        for ns in v1.list_namespace(label_selector={"match_labels": match_labels}):
            items.extend(v1.list_namespaced_config_map(namespace=ns))
    else:
        ret = v1.list_namespaced_config_map(namespace=namespace)
    for cm in ret.items:
        destFolder = targetFolder
        metadata = cm.metadata
        if metadata.labels is None:
            continue
        print(f'Working on configmap {metadata.namespace}/{metadata.name}')
        if label in cm.metadata.labels.keys():
            print("Configmap with label found")
            if cm.metadata.annotations is not None:
                if folderAnnotation in cm.metadata.annotations.keys():
                    destFolder = cm.metadata.annotations[folderAnnotation]

            dataMap=cm.data
            if dataMap is None:
                print("Configmap does not have data.")
                continue
            if label in cm.metadata.labels.keys():
                for filename in dataMap.keys():
                    fileData = dataMap[filename]
                    if filename.endswith(".url"):
                        filename = filename[:-4]
                        fileData = request(fileData, "GET").text
                    writeTextToFile(destFolder, filename, fileData)
                    if url is not None:
                        request(url, method, payload)


def watchForChanges(label, targetFolder, url, method, payload, current, folderAnnotation):
    v1 = client.CoreV1Api()
    w = watch.Watch()
    stream = None
    namespace = os.getenv("NAMESPACE")
    namespace_label_selector = os.getenv("NAMESPACE_LABELS")
    if namespace is None and namespace_label_selector is None:
        stream = w.stream(v1.list_namespaced_config_map, namespace=current)
    elif namespace == "ALL":
        stream = w.stream(v1.list_config_map_for_all_namespaces)
    elif namespace_label_selector:
        match_labels = dict(m.split("=") for m in namespace_label_selector.split(","))
        queue = queue.Queue()
        workers = []

        def queue_worker(list_func, *args, **kwargs):
            for event in watch.Watch().stream(list_func, *args, **kwargs):
                queue.put(event)

        for ns in v1.list_namespace(label_selector={"match_labels": match_labels}):
            t = threading.Thread(
                daemon=True,
                target=queue_worker,
                name='watcher-{}'.format(ns),
                args=(v1.list_namespaced_config_map,),
                kwargs={"namespace": ns},
            )
            workers.append(t)
            t.start()
        
        def event_stream():
            while True:
                yield queue.get()
        stream = event_stream()
    else:
        stream = w.stream(v1.list_namespaced_config_map, namespace=namespace)
    for event in stream:
        destFolder = targetFolder
        metadata = event['object'].metadata
        if metadata.labels is None:
            continue
        print(f'Working on configmap {metadata.namespace}/{metadata.name}')
        if label in event['object'].metadata.labels.keys():
            print("Configmap with label found")
            if event['object'].metadata.annotations is not None:
                if folderAnnotation in event['object'].metadata.annotations.keys():
                    destFolder = event['object'].metadata.annotations[folderAnnotation]
                    print(f'Found a folder override annotation, placing the configmap in: {destFolder}')
            dataMap=event['object'].data
            if dataMap is None:
                print("Configmap does not have data.")
                continue
            eventType = event['type']
            for filename in dataMap.keys():
                print("File in configmap %s %s" % (filename, eventType))
                if (eventType == "ADDED") or (eventType == "MODIFIED"):
                    fileData = dataMap[filename]
                    if filename.endswith(".url"):
                        filename = filename[:-4]
                        fileData = request(fileData, "GET").text
                    writeTextToFile(destFolder, filename, fileData)
                    if url is not None:
                        request(url, method, payload)
                else:
                    if filename.endswith(".url"):
                        filename = filename[:-4]
                    removeFile(destFolder, filename)
                    if url is not None:
                        request(url, method, payload)


def main():
    print("Starting config map collector")
    folderAnnotation = os.getenv('FOLDER_ANNOTATIONS')
    if folderAnnotation is None:
        print("No folder annotation was provided, defaulting to k8s-sidecar-target-directory")
        folderAnnotation = "k8s-sidecar-target-directory"
    label = os.getenv('LABEL')
    if label is None:
        print("Should have added LABEL as environment variable! Exit")
        return -1
    targetFolder = os.getenv('FOLDER')
    if targetFolder is None:
        print("Should have added FOLDER as environment variable! Exit")
        return -1

    method = os.getenv('REQ_METHOD')
    url = os.getenv('REQ_URL')
    payload = os.getenv('REQ_PAYLOAD')

    config.load_incluster_config()
    print("Config for cluster api loaded...")
    namespace = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read()

    if os.getenv('SKIP_TLS_VERIFY') == 'true':
        configuration = client.Configuration()
        configuration.verify_ssl=False
        configuration.debug = False
        client.Configuration.set_default(configuration)

    k8s_method = os.getenv("METHOD")    
    if k8s_method == "LIST":
        listConfigmaps(label, targetFolder, url, method, payload, namespace, folderAnnotation)
    else:
        while True:
            try:
                watchForChanges(label, targetFolder, url, method, payload, namespace, folderAnnotation)
            except ApiException as e:
                if e.status is not 500:
                  print("ApiException when calling kubernetes: %s\n" % e)
                else:
                  raise
            except ProtocolError as e:
                print("ProtocolError when calling kubernetes: %s\n" % e)
            except:
                raise


if __name__ == '__main__':
    main()
