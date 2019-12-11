import logging

from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor

from libcloud.compute.types import Provider
from libcloud.compute.providers import get_driver
from libcloud.common.google import GoogleBaseError

from flambe.cluster.errors import ClusterError
from flambe.cluster.cluster import Cluster
from flambe.cluster.instance import OrchestratorInstance, CPUFactoryInstance, GPUFactoryInstance


logger = logging.getLogger(__name__)


class GCPCluster(Cluster):

    def __init__(self,
                 name: str,
                 factory_type: str,
                 factories_num: int,
                 orchestrator_type: str,
                 ssh_key: str,
                 ssh_username: str,
                 service_account_email: str,
                 service_account_key: str,
                 project_id: str,
                 zone: str = 'us-central1-a',
                 factory_image: Optional[str] = None,
                 gpu_type: Optional[str] = None,
                 gpu_count: int = 1,
                 orchestrator_image: Optional[str] = None,
                 setup_cmds: Optional[List[str]] = None) -> None:
        super().__init__(name, factories_num, ssh_key, ssh_username, setup_cmds)
        self.factory_type = factory_type
        self.orchestrator_type = orchestrator_type
        self.driver = get_driver(Provider.GCE)
        self.service_account_email = service_account_email
        self.service_account_key = service_account_key
        self.project_id = project_id
        self.zone = zone
        self.conn = self.driver(
            self.service_account_email,
            key=self.service_account_key,
            datacenter=self.zone,
            project=self.project_id
        )

        self.factory_image = factory_image
        if self.factory_image is None:
            self.factory_image = self.conn.ex_get_image_from_family(
                'pytorch-1-1-cpu', ex_project_list=['deeplearning-platform-release'])
        self.orchestrator_image = orchestrator_image
        if self.orchestrator_image is None:
            self.orchestrator_image = self.conn.ex_get_image_from_family(
                'pytorch-1-1-cpu', ex_project_list=['deeplearning-platform-release'])
        self.gpu_type = gpu_type
        self.gpu_count = gpu_count

    def _create_cpu_factory(self, name: str) -> CPUFactoryInstance:
        node = self.conn.create_node(name, self.factory_type, self.factory_image)
        return CPUFactoryInstance(
            node.public_ips[0],
            node.private_ips[0],
            self.username,
            self.key,
            self.config,
            self.debug,
        )

    def _create_gpu_factory(self, name: str) -> GPUFactoryInstance:
        node = self.conn.create_node(
            name,
            self.factory_type,
            self.factory_image,
            ex_accelerator_type=self.gpu_type,
            ex_accelerator_count=self.gpu_count,
            ex_on_host_maintenance='TERMINATE',
            ex_automatic_restart=True,
        )
        return GPUFactoryInstance(
            node.public_ips[0],
            node.private_ips[0],
            self.username,
            self.key,
            self.config,
            self.debug,
        )

    def load_all_instances(self) -> None:
        with ThreadPoolExecutor() as executor:
            # launch the orchestrator
            logger.info("Launching the orchestrator")
            future_orchestrator_node = executor.submit(
                self.conn.create_node,
                self.get_orchestrator_name(),
                self.orchestrator_type,
                self.orchestrator_image
            )

            # launch factories
            if self.gpu_type is None:
                logger.info("Launching the CPU factories")
                future_factories = executor.map(
                    self._create_cpu_factory,
                    [self.get_factory_basename() + f'-{i+1}' for i in range(self.factories_num)],
                )
            else:
                logger.info("Launching the GPU factories")
                future_factories = executor.map(
                    self._create_gpu_factory,
                    [self.get_factory_basename() + f'-{i+1}' for i in range(self.factories_num)],
                )
            try:
                orchestrator_node = future_orchestrator_node.result()
                self.orchestrator = OrchestratorInstance(
                    orchestrator_node.public_ips[0],
                    orchestrator_node.private_ips[0],
                    self.username,
                    self.key,
                    self.config,
                    self.debug,
                )

                self.factories = [f for f in future_factories]

            except GoogleBaseError as e:
                raise ClusterError(f"Error creating nodes. Original error: {e}")

    def get_orchestrator_name(self) -> str:
        return f"{self.name}-orchestrator"

    def get_factory_basename(self) -> str:
        return f"{self.name}-factory"

    def rollback_env(self) -> None:
        return super().rollback_env()
