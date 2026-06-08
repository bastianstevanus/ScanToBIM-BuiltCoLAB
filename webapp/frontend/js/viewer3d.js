/**
 * viewer3d.js — Three.js 3D scene manager.
 *
 * Usage:
 *   const viewer = new Viewer3D('viewer-canvas');
 *   viewer.loadLayer('scan', positionsF32, colorsF32);
 *   viewer.loadLayer('bim',  positionsF32, colorsF32);
 *   viewer.showLayer('scan', true);
 *   viewer.centerCamera();
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// IFC + scan data canonically use Z-up (floor lies on the XY plane).
// Make the whole viewer Z-up so geometry is shown with its real orientation.
THREE.Object3D.DEFAULT_UP = new THREE.Vector3(0, 0, 1);

export class Viewer3D {
  constructor(canvasId) {
    this._canvas   = document.getElementById(canvasId);
    this._layers   = {};        // name → THREE.Points
    this._meshes   = {};        // name → THREE.Mesh (for IFC mesh layers)
    this._visible  = { scan: true, bim: true };

    this._initRenderer();
    this._initScene();
    this._initCamera();
    this._initControls();
    this._initLights();
    this._initResize();
    this._animate();
  }

  // ── Initialisation ──────────────────────────────────────────────────────────

  _initRenderer() {
    this._renderer = new THREE.WebGLRenderer({
      canvas: this._canvas,
      antialias: true,
    });
    this._renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this._renderer.setClearColor(0x080c12);
    this._updateSize();
  }

  _initScene() {
    this._scene = new THREE.Scene();
    // Subtle ambient grid on the floor (XY plane, Z-up convention).
    // GridHelper is built in XZ by default — rotate it so it lies flat on XY.
    const grid = new THREE.GridHelper(200, 40, 0x1a1a2e, 0x16213e);
    grid.rotation.x = Math.PI / 2;
    grid.position.z = -0.01;
    this._scene.add(grid);
  }

  _initCamera() {
    const { w, h } = this._dims();
    this._camera = new THREE.PerspectiveCamera(55, w / h, 0.01, 5000);
    this._camera.up.set(0, 0, 1);              // Z-up
    // Iso-ish view looking down at the XY floor from +Y, +Z.
    this._camera.position.set(0, -60, 30);
    this._camera.lookAt(0, 0, 0);
  }

  _initControls() {
    this._controls = new OrbitControls(this._camera, this._renderer.domElement);
    this._controls.enableDamping    = true;
    this._controls.dampingFactor    = 0.08;
    this._controls.screenSpacePanning = false;
    this._controls.minDistance = 0.5;
    this._controls.maxDistance = 2000;
  }

  _initLights() {
    this._scene.add(new THREE.AmbientLight(0xffffff, 0.4));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    // Light from above in Z-up world.
    dir.position.set(50, 50, 100);
    this._scene.add(dir);
  }

  _initResize() {
    const ro = new ResizeObserver(() => {
      this._updateSize();
    });
    ro.observe(this._canvas.parentElement);
  }

  _updateSize() {
    const el = this._canvas.parentElement;
    const w  = el ? el.clientWidth  : window.innerWidth;
    const h  = el ? el.clientHeight : window.innerHeight;
    this._renderer.setSize(w, h, false);
    if (this._camera) {
      this._camera.aspect = w / h;
      this._camera.updateProjectionMatrix();
    }
  }

  _dims() {
    const el = this._canvas.parentElement;
    return { w: el ? el.clientWidth : window.innerWidth,
             h: el ? el.clientHeight : window.innerHeight };
  }

  // ── Animate ─────────────────────────────────────────────────────────────────

  _animate() {
    requestAnimationFrame(() => this._animate());
    this._controls.update();
    this._renderer.render(this._scene, this._camera);
  }

  // ── Layer API ───────────────────────────────────────────────────────────────

  /**
   * Create / replace a point-cloud layer.
   * @param {string}       name      - 'scan' | 'bim' | any unique key
   * @param {Float32Array} positions - flat [x0,y0,z0, x1,y1,z1, …]
   * @param {Float32Array} colors    - flat [r0,g0,b0, r1,g1,b1, …] in [0,1]
   * @param {number}       pointSize - world-space point size (default 0.04)
   */
  loadLayer(name, positions, colors, pointSize = 0.04) {
    // Remove old layer
    if (this._layers[name]) {
      this._scene.remove(this._layers[name]);
      this._layers[name].geometry.dispose();
      this._layers[name].material.dispose();
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('color',    new THREE.BufferAttribute(colors,    3));

    const mat = new THREE.PointsMaterial({
      size:             pointSize,
      vertexColors:     true,
      sizeAttenuation:  true,
    });

    const pts = new THREE.Points(geo, mat);
    pts.visible = this._visible[name] !== false;
    this._scene.add(pts);
    this._layers[name] = pts;
  }

  /**
   * Update only the colours of an existing layer (no re-upload of positions).
   */
  updateColors(name, colors) {
    const layer = this._layers[name];
    if (!layer) return;
    layer.geometry.attributes.color.array.set(colors);
    layer.geometry.attributes.color.needsUpdate = true;
  }

  /** Show or hide a named layer (works for both point clouds and meshes). */
  showLayer(name, visible) {
    this._visible[name] = visible;
    if (this._layers[name]) this._layers[name].visible = visible;
    if (this._meshes[name]) this._meshes[name].visible = visible;
  }

  /**
   * Load an IFC mesh layer (THREE.Mesh with vertex colours).
   * positions = flat Float32Array of non-indexed triangle vertices [x0,y0,z0, ...]
   * colors    = flat Float32Array of per-vertex colours [r0,g0,b0, ...]
   */
  loadMesh(name, positions, colors) {
    // Remove old
    if (this._meshes[name]) {
      this._scene.remove(this._meshes[name]);
      this._meshes[name].geometry.dispose();
      this._meshes[name].material.dispose();
    }
    if (this._layers[name]) {
      this._scene.remove(this._layers[name]);
      this._layers[name].geometry.dispose();
      this._layers[name].material.dispose();
      delete this._layers[name];
    }

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('color',    new THREE.BufferAttribute(colors,    3));
    geo.computeVertexNormals();

    const mat = new THREE.MeshBasicMaterial({
      vertexColors: true,
      side: THREE.DoubleSide,
    });

    const mesh = new THREE.Mesh(geo, mat);
    mesh.visible = this._visible[name] !== false;
    this._scene.add(mesh);
    this._meshes[name] = mesh;
  }

  /** Remove all point-cloud layers. */
  clearAll() {
    for (const pts of Object.values(this._layers)) {
      this._scene.remove(pts);
      pts.geometry.dispose();
      pts.material.dispose();
    }
    for (const mesh of Object.values(this._meshes)) {
      this._scene.remove(mesh);
      mesh.geometry.dispose();
      mesh.material.dispose();
    }
    this._layers = {};
    this._meshes = {};
  }

  /**
   * Auto-position the camera to frame all visible geometry.
   */
  centerCamera() {
    const box = new THREE.Box3();
    let empty = true;

    for (const pts of Object.values(this._layers)) {
      if (!pts.visible) continue;
      pts.geometry.computeBoundingBox();
      if (pts.geometry.boundingBox) {
        box.union(pts.geometry.boundingBox);
        empty = false;
      }
    }
    for (const mesh of Object.values(this._meshes)) {
      if (!mesh.visible) continue;
      mesh.geometry.computeBoundingBox();
      if (mesh.geometry.boundingBox) {
        box.union(mesh.geometry.boundingBox);
        empty = false;
      }
    }

    if (empty) return;

    const center = new THREE.Vector3();
    box.getCenter(center);
    const size   = box.getSize(new THREE.Vector3()).length();
    this._controls.maxDistance = Math.max(2000, size * 2);

    this._controls.target.copy(center);
    // Z-up framing: place camera offset along -Y (in front) and +Z (above).
    this._camera.position.set(
      center.x + size * 0.4,
      center.y - size * 0.7,
      center.z + size * 0.5,
    );
    this._camera.lookAt(center);
    this._controls.update();

    this._camera.near = size * 0.001;
    this._camera.far  = size * 10;
    this._camera.updateProjectionMatrix();

    // Adjust grid to lie on the scene floor (Z = box.min.z, XY plane).
    const grid = this._scene.children.find(c => c.isGridHelper);
    if (grid) {
      grid.rotation.x = Math.PI / 2;
      grid.position.set(center.x, center.y, box.min.z - 0.01);
      grid.scale.setScalar(size / 200);
    }
  }

  /**
   * Draw a jet-colourmap gradient on a canvas element (for the legend).
   * @param {HTMLCanvasElement} canvas
   * @param {number} maxDevM  - max deviation in metres (for tick labels)
   */
  static drawJetLegend(canvas, maxDevM = 0) {
    const ctx = canvas.getContext('2d');
    const w   = canvas.width;
    const h   = canvas.height;
    const gradient = ctx.createLinearGradient(0, 0, w, 0);

    // Jet: blue→cyan→green→yellow→red
    gradient.addColorStop(0.0,  '#0000ff');
    gradient.addColorStop(0.25, '#00ffff');
    gradient.addColorStop(0.5,  '#00ff00');
    gradient.addColorStop(0.75, '#ffff00');
    gradient.addColorStop(1.0,  '#ff0000');

    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, w, h);

    // Tick lines
    ctx.strokeStyle = 'rgba(255,255,255,0.6)';
    ctx.lineWidth   = 1;
    const ticks = [0, 0.25, 0.5, 0.75, 1.0];
    for (const t of ticks) {
      const x = Math.round(t * (w - 1));
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
  }
}
