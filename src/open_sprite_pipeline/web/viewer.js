let THREE = null;
let OrbitControls = null;
let GLTFLoader = null;
let MTLLoader = null;
let OBJLoader = null;

export async function loadViewerModules() {
  const [threeModule, controlsModule, gltfModule, mtlModule, objModule] = await Promise.all([
    import("three"),
    import("three/addons/controls/OrbitControls.js"),
    import("three/addons/loaders/GLTFLoader.js"),
    import("three/addons/loaders/MTLLoader.js"),
    import("three/addons/loaders/OBJLoader.js"),
  ]);
  THREE = threeModule;
  OrbitControls = controlsModule.OrbitControls;
  GLTFLoader = gltfModule.GLTFLoader;
  MTLLoader = mtlModule.MTLLoader;
  OBJLoader = objModule.OBJLoader;
}

export class MeshViewer {
  constructor(container) {
    this.container = container;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.01, 100);
    this.camera.position.set(2.4, 1.4, 2.8);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.1;
    this.container.innerHTML = "";
    this.container.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.autoRotate = true;
    this.controls.autoRotateSpeed = 1.1;
    this.scene.add(new THREE.HemisphereLight(0xe8f4dc, 0x26312a, 2.3));
    const key = new THREE.DirectionalLight(0xffffff, 3.2);
    key.position.set(3, 4, 5);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0xb7f174, 1.5);
    rim.position.set(-4, 2, -3);
    this.scene.add(rim);
    this.object = null;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.container);
    this.resize();
    this.animate();
  }

  resize() {
    const width = Math.max(1, this.container.clientWidth);
    const height = Math.max(1, this.container.clientHeight);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  disposeObject() {
    if (!this.object) return;
    this.scene.remove(this.object);
    this.object.traverse((child) => {
      child.geometry?.dispose?.();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      for (const material of materials) {
        if (!material) continue;
        for (const value of Object.values(material)) {
          if (value?.isTexture) value.dispose();
        }
        material.dispose?.();
      }
    });
    this.object = null;
  }

  async load(url, assetType, materialUrl = null) {
    this.disposeObject();
    const extension = (assetType || url.split(".").pop() || "").toLowerCase();
    let object;
    if (extension === "glb" || extension === "gltf") {
      const gltf = await new GLTFLoader().loadAsync(url);
      object = gltf.scene;
    } else if (extension === "obj") {
      const loader = new OBJLoader();
      if (materialUrl) {
        const materials = await new MTLLoader().loadAsync(materialUrl);
        materials.preload();
        loader.setMaterials(materials);
      }
      object = await loader.loadAsync(url);
      if (!materialUrl) {
        object.traverse((child) => {
          if (child.isMesh) child.material = new THREE.MeshStandardMaterial({ color: 0xb7f174, roughness: .65, metalness: .05, side: THREE.DoubleSide });
        });
      }
    } else {
      throw new Error(`The browser viewer does not yet support .${extension} assets.`);
    }
    object.traverse((child) => {
      if (child.isMesh) {
        child.castShadow = true;
        child.receiveShadow = true;
        if (child.material) child.material.side = THREE.DoubleSide;
      }
    });
    this.object = object;
    this.scene.add(object);
    this.fit(object);
  }

  fit(object) {
    const box = new THREE.Box3().setFromObject(object);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    object.position.sub(center);
    const radius = Math.max(size.x, size.y, size.z, .2);
    this.camera.position.set(radius * 1.7, radius * 1.05, radius * 2.15);
    this.camera.near = Math.max(.001, radius / 100);
    this.camera.far = radius * 100;
    this.camera.updateProjectionMatrix();
    this.controls.target.set(0, 0, 0);
    this.controls.update();
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}
